"""Mission planning: goal -> task graph.

The planner is itself an agent (`planner.yaml`), so its prompt is tunable
without touching code. Its output is parsed defensively: a model returning
malformed JSON, an unknown agent id, or a cyclic dependency must degrade to a
usable single-task plan rather than failing the mission.

Without a provider key the echo engine cannot plan at all, so planning falls
back to one task on the default agent -- honest degradation instead of a
fabricated graph.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agents.registry import DEFAULT_AGENT_ID, PLANNER_AGENT_ID, registry as agent_registry
from app.agents.runner import AgentRunner
from app.missions.models import Budget, Task, TaskStatus, new_task_id

log = logging.getLogger("jarvis.planner")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or code fences often enough that this is the
    normal path, not an edge case. Tries the whole string first, then the
    outermost braced span.
    """
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    candidates = [cleaned]
    match = _JSON_BLOCK.search(cleaned)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def single_task_plan(mission_id: str, goal: str, agent_id: str = DEFAULT_AGENT_ID) -> list[Task]:
    """The fallback plan: do it in one task."""
    return [
        Task(
            task_id=new_task_id(),
            mission_id=mission_id,
            agent_id=agent_id,
            objective=goal,
            status=TaskStatus.READY,
            depth=0,
            idempotency_key=f"{agent_id}:{goal}"[:200],
        )
    ]


def _topologically_valid(tasks: list[dict[str, Any]]) -> bool:
    """Reject cycles and dangling references before they reach the scheduler."""
    ids = {t["id"] for t in tasks}
    for task in tasks:
        for dependency in task.get("depends_on", []):
            if dependency not in ids or dependency == task["id"]:
                return False

    # Kahn's algorithm: if anything is left over, there is a cycle.
    pending = {t["id"]: set(t.get("depends_on", [])) for t in tasks}
    while pending:
        ready = [tid for tid, deps in pending.items() if not deps]
        if not ready:
            return False
        for tid in ready:
            del pending[tid]
        for deps in pending.values():
            deps.difference_update(ready)
    return True


async def plan_mission(
    *,
    mission_id: str,
    goal: str,
    runner: AgentRunner,
    budget: Budget,
) -> tuple[list[Task], str]:
    """Produce the task graph for a goal. Returns (tasks, planner notes)."""
    definition = agent_registry.get(PLANNER_AGENT_ID)
    if definition is None:
        return single_task_plan(mission_id, goal), "planner agent unavailable"

    roster = agent_registry.roster_for_planner()
    objective = (
        f"User goal:\n{goal}\n\n"
        f"Available agents:\n{roster}\n\n"
        f"Emit at most {budget.max_tasks} tasks."
    )

    try:
        result = await runner.run(definition, objective=objective, mission_id=mission_id)
    except Exception as exc:  # noqa: BLE001 - planning must never kill the mission
        log.warning("planner failed, falling back to single task: %s", exc)
        return single_task_plan(mission_id, goal), f"planner failed: {exc}"

    if result.provider == "ECHO":
        return (
            single_task_plan(mission_id, goal),
            "No model key configured, so the goal was not decomposed.",
        )

    parsed = _extract_json(result.text)
    if not parsed or not isinstance(parsed.get("tasks"), list) or not parsed["tasks"]:
        log.warning("planner returned unusable output, falling back to single task")
        return single_task_plan(mission_id, goal), "planner output was not usable"

    raw_tasks = parsed["tasks"][: budget.max_tasks]
    valid_agents = set(agent_registry.ids()) - {PLANNER_AGENT_ID}

    normalised: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict) or not raw.get("objective"):
            continue
        agent_id = raw.get("agent")
        if agent_id not in valid_agents:
            log.warning("planner named unknown agent '%s'; using %s", agent_id, DEFAULT_AGENT_ID)
            agent_id = DEFAULT_AGENT_ID
        normalised.append(
            {
                "id": str(raw.get("id") or f"t{index + 1}"),
                "agent": agent_id,
                "objective": str(raw["objective"]),
                "depends_on": [str(d) for d in (raw.get("depends_on") or [])],
            }
        )

    if not normalised:
        return single_task_plan(mission_id, goal), "planner produced no usable tasks"

    if not _topologically_valid(normalised):
        log.warning("planner produced a cyclic or dangling graph; flattening dependencies")
        for task in normalised:
            task["depends_on"] = []

    # Map planner-local ids ("t1") onto real task ids.
    id_map = {task["id"]: new_task_id() for task in normalised}
    tasks = [
        Task(
            task_id=id_map[task["id"]],
            mission_id=mission_id,
            agent_id=task["agent"],
            objective=task["objective"],
            status=TaskStatus.PENDING if task["depends_on"] else TaskStatus.READY,
            depth=0,
            depends_on=[id_map[d] for d in task["depends_on"] if d in id_map],
            idempotency_key=f"{task['agent']}:{task['objective']}"[:200],
        )
        for task in normalised
    ]

    return tasks, str(parsed.get("notes") or "")
