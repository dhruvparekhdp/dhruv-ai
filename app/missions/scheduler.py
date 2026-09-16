"""Mission scheduler -- the only thing that dispatches work.

Centralising dispatch here is what makes the spec's anti-recursion rule
structural rather than aspirational: agents have no way to start work, so every
delegation passes through one audited place that can refuse it.

Execution model: repeatedly find tasks whose dependencies have succeeded, run
them concurrently, record results, repeat. Budgets are checked before every
dispatch so an exhausted mission fails with a reason instead of hanging.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.registry import DEFAULT_AGENT_ID, registry as agent_registry
from app.agents.runner import AgentRunner
from app.missions import store
from app.missions.models import (
    ApprovalDecision,
    Budget,
    Mission,
    MissionStatus,
    Task,
    TaskStatus,
    new_mission_id,
)
from app.missions.planner import plan_mission
from app.services.websocket_manager import ws_manager

log = logging.getLogger("jarvis.scheduler")

#: How long a task may hold its lease before reconciliation reclaims it.
LEASE_SECONDS = 180
#: How long to wait for a human approval decision before giving up.
APPROVAL_TIMEOUT_SECONDS = 600
APPROVAL_POLL_SECONDS = 2


class MissionScheduler:
    def __init__(self, runner: AgentRunner | None = None) -> None:
        self.runner = runner or AgentRunner()
        self._running: dict[str, asyncio.Task[Any]] = {}

    # -- lifecycle -------------------------------------------------------

    async def create_mission(
        self, *, goal: str, session_id: str = "", budget: Budget | None = None
    ) -> Mission:
        mission = Mission(
            mission_id=new_mission_id(),
            goal=goal,
            session_id=session_id,
            budget=budget or Budget(),
            status=MissionStatus.PLANNING,
        )
        store.create_mission(mission)
        store.log_message(
            mission_id=mission.mission_id,
            sender="user",
            receiver="orchestrator",
            action="mission.create",
            payload={"goal": goal},
        )
        await ws_manager.broadcast(
            "mission", f"Mission created: {goal[:80]}",
            mission_id=mission.mission_id, status=mission.status.value,
        )
        return mission

    def start(self, mission_id: str) -> None:
        """Run a mission in the background so the API returns immediately."""
        if mission_id in self._running:
            return
        task = asyncio.create_task(self._run_mission(mission_id))
        self._running[mission_id] = task
        task.add_done_callback(lambda _: self._running.pop(mission_id, None))

    async def wait_for(self, mission_id: str, timeout: float = 60.0) -> None:
        """Block until a mission finishes. Used by tests and synchronous callers."""
        task = self._running.get(mission_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def cancel(self, mission_id: str) -> bool:
        mission = store.get_mission(mission_id)
        if mission is None or mission.is_terminal:
            return False

        task = self._running.get(mission_id)
        if task is not None:
            task.cancel()

        for pending in store.list_tasks(mission_id):
            if not pending.is_terminal:
                store.update_task(pending.task_id, status=TaskStatus.CANCELLED, clear_lease=True)
        store.update_mission(mission_id, status=MissionStatus.CANCELLED, error="cancelled by user")
        await ws_manager.broadcast(
            "mission", "Mission cancelled", mission_id=mission_id, status="CANCELLED"
        )
        return True

    # -- execution -------------------------------------------------------

    async def _run_mission(self, mission_id: str) -> None:
        started = time.perf_counter()
        mission = store.get_mission(mission_id)
        if mission is None:
            return

        try:
            tasks = store.list_tasks(mission_id)
            if not tasks:
                tasks, notes = await plan_mission(
                    mission_id=mission_id,
                    goal=mission.goal,
                    runner=self.runner,
                    budget=mission.budget,
                )
                store.create_tasks(tasks)
                store.log_message(
                    mission_id=mission_id,
                    sender="planner",
                    receiver="orchestrator",
                    action="plan.created",
                    payload={"tasks": len(tasks), "notes": notes},
                )
                await ws_manager.broadcast(
                    "mission", f"Planned {len(tasks)} task(s)",
                    mission_id=mission_id, tasks=len(tasks), notes=notes,
                )

            store.update_mission(mission_id, status=MissionStatus.RUNNING)
            await self._execute_graph(mission, started)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a mission must never take the server down
            log.exception("mission %s crashed", mission_id)
            store.update_mission(mission_id, status=MissionStatus.FAILED, error=str(exc))
            await ws_manager.broadcast(
                "error", f"Mission failed: {exc}", mission_id=mission_id, status="FAILED"
            )

    async def _execute_graph(self, mission: Mission, started: float) -> None:
        budget = mission.budget
        mission_id = mission.mission_id
        tokens_used = 0
        tool_calls_used = 0

        while True:
            tasks = store.list_tasks(mission_id)

            if all(task.is_terminal for task in tasks):
                await self._finish(mission_id, tasks)
                return

            # Budget checks happen before dispatch so exhaustion is a clean
            # reported outcome, never a half-finished mission that hangs.
            elapsed = time.perf_counter() - started
            exhausted = self._budget_exhausted(
                budget, elapsed=elapsed, tokens=tokens_used,
                tool_calls=tool_calls_used, task_count=len(tasks),
            )
            if exhausted:
                await self._abort(mission_id, tasks, exhausted)
                return

            ready = self._ready_tasks(tasks)
            if not ready:
                blocked = self._resolve_blocked(tasks)
                if blocked:
                    continue          # dependencies failed; tasks were skipped
                await self._finish(mission_id, store.list_tasks(mission_id))
                return

            results = await asyncio.gather(
                *(self._execute_task(mission, task) for task in ready),
                return_exceptions=True,
            )
            for outcome in results:
                if isinstance(outcome, BaseException):
                    log.error("task execution raised: %s", outcome)
                    continue
                tokens_used += outcome.get("tokens", 0)
                tool_calls_used += outcome.get("tool_calls", 0)

    def _budget_exhausted(
        self, budget: Budget, *, elapsed: float, tokens: int, tool_calls: int, task_count: int
    ) -> str:
        if elapsed > budget.max_seconds:
            return f"time budget exceeded ({budget.max_seconds}s)"
        if tokens > budget.max_tokens:
            return f"token budget exceeded ({budget.max_tokens})"
        if tool_calls > budget.max_tool_calls:
            return f"tool-call budget exceeded ({budget.max_tool_calls})"
        if task_count > budget.max_tasks:
            return f"task budget exceeded ({budget.max_tasks})"
        return ""

    def _ready_tasks(self, tasks: list[Task]) -> list[Task]:
        by_id = {task.task_id: task for task in tasks}
        ready: list[Task] = []
        for task in tasks:
            if task.status not in (TaskStatus.READY, TaskStatus.PENDING):
                continue
            dependencies = [by_id.get(d) for d in task.depends_on]
            if all(d is not None and d.status is TaskStatus.SUCCESS for d in dependencies):
                ready.append(task)
        return ready

    def _resolve_blocked(self, tasks: list[Task]) -> bool:
        """Skip tasks whose dependencies can never succeed. Returns True if any changed."""
        by_id = {task.task_id: task for task in tasks}
        changed = False
        for task in tasks:
            if task.is_terminal:
                continue
            for dependency_id in task.depends_on:
                dependency = by_id.get(dependency_id)
                if dependency is None or dependency.status in (
                    TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED
                ):
                    store.update_task(
                        task.task_id,
                        status=TaskStatus.SKIPPED,
                        error=f"dependency {dependency_id} did not succeed",
                    )
                    changed = True
                    break
        return changed

    async def _execute_task(self, mission: Mission, task: Task) -> dict[str, int]:
        definition = agent_registry.get(task.agent_id) or agent_registry.get(DEFAULT_AGENT_ID)
        if definition is None:
            store.update_task(task.task_id, status=TaskStatus.FAILED, error="no agent available")
            return {"tokens": 0, "tool_calls": 0}

        # Skip work an earlier run already completed (crash-safety).
        duplicate = store.find_task_by_idempotency_key(mission.mission_id, task.idempotency_key)
        if duplicate is not None and duplicate.task_id != task.task_id:
            store.update_task(
                task.task_id,
                status=TaskStatus.SUCCESS,
                result=duplicate.result,
                error="deduplicated: identical task already succeeded",
            )
            return {"tokens": 0, "tool_calls": 0}

        store.lease_task(task.task_id, LEASE_SECONDS)
        store.update_task(task.task_id, attempts=task.attempts + 1)
        store.log_message(
            mission_id=mission.mission_id,
            task_id=task.task_id,
            sender="orchestrator",
            receiver=definition.id,
            action="task.dispatch",
            payload={"objective": task.objective},
        )
        await ws_manager.broadcast(
            "task", f"{definition.name}: {task.objective[:70]}",
            mission_id=mission.mission_id, task_id=task.task_id,
            agent=definition.id, status="RUNNING",
        )

        context = self._context_for(task)

        try:
            outcome = await self.runner.run(
                definition,
                objective=task.objective,
                context=context,
                mission_id=mission.mission_id,
                approval_hook=self._approval_hook(mission, task, definition.id),
                on_event=self._event_hook(mission.mission_id, task.task_id),
            )
        except Exception as exc:  # noqa: BLE001 - one task failing must not kill the mission
            log.exception("task %s failed", task.task_id)
            store.update_task(
                task.task_id, status=TaskStatus.FAILED, error=str(exc), clear_lease=True
            )
            store.log_message(
                mission_id=mission.mission_id, task_id=task.task_id,
                sender=definition.id, receiver="orchestrator",
                action="task.failed", payload={"error": str(exc)}, status="FAILED",
            )
            await ws_manager.broadcast(
                "task", f"{definition.name} failed: {exc}",
                mission_id=mission.mission_id, task_id=task.task_id, status="FAILED",
            )
            return {"tokens": 0, "tool_calls": 0}

        store.update_task(
            task.task_id,
            status=TaskStatus.SUCCESS,
            result=outcome.text,
            clear_lease=True,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            tool_calls=outcome.tool_calls,
        )
        store.log_message(
            mission_id=mission.mission_id, task_id=task.task_id,
            sender=definition.id, receiver="orchestrator",
            action="task.result",
            payload={"result": outcome.text[:500], "tool_calls": outcome.tool_calls},
            status="SUCCESS",
        )
        await ws_manager.broadcast(
            "task", f"{definition.name} finished",
            mission_id=mission.mission_id, task_id=task.task_id,
            status="SUCCESS", provider=outcome.provider,
        )
        return {"tokens": outcome.total_tokens, "tool_calls": outcome.tool_calls}

    def _context_for(self, task: Task) -> str:
        """Assemble context from this task's dependencies only.

        Explicitly scoped: a task sees what it depends on and nothing else, so
        unrelated missions -- and unrelated branches of this one -- cannot leak in.
        """
        if not task.depends_on:
            return ""
        chunks = []
        for dependency_id in task.depends_on:
            dependency = store.get_task(dependency_id)
            if dependency and dependency.result:
                chunks.append(f"[{dependency.agent_id}] {dependency.result}")
        return "\n\n".join(chunks)

    # -- approval --------------------------------------------------------

    def _approval_hook(self, mission: Mission, task: Task, agent_id: str):
        async def hook(tool_name: str, arguments: dict[str, Any]) -> bool:
            approval_id = store.create_approval(
                mission_id=mission.mission_id,
                task_id=task.task_id,
                agent_id=agent_id,
                tool_name=tool_name,
                tool_arguments=arguments,
                risk="DANGEROUS",
            )
            store.update_task(task.task_id, status=TaskStatus.NEEDS_APPROVAL)
            store.update_mission(mission.mission_id, status=MissionStatus.NEEDS_APPROVAL)
            await ws_manager.broadcast(
                "approval",
                f"Approval needed: {agent_id} wants to run {tool_name}",
                mission_id=mission.mission_id, task_id=task.task_id,
                approval_id=approval_id, tool=tool_name, arguments=arguments,
            )

            deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(APPROVAL_POLL_SECONDS)
                record = store.get_approval(approval_id)
                if record and record["decision"] != ApprovalDecision.PENDING.value:
                    approved = record["decision"] == ApprovalDecision.APPROVED.value
                    store.update_task(task.task_id, status=TaskStatus.RUNNING)
                    store.update_mission(mission.mission_id, status=MissionStatus.RUNNING)
                    await ws_manager.broadcast(
                        "approval",
                        f"{tool_name} {'approved' if approved else 'denied'}",
                        mission_id=mission.mission_id, approval_id=approval_id, approved=approved,
                    )
                    return approved

            store.decide_approval(approval_id, ApprovalDecision.DENIED, decided_by="timeout")
            store.update_task(task.task_id, status=TaskStatus.RUNNING)
            store.update_mission(mission.mission_id, status=MissionStatus.RUNNING)
            return False

        return hook

    def _event_hook(self, mission_id: str, task_id: str):
        async def hook(kind: str, message: str, payload: dict[str, Any]) -> None:
            await ws_manager.broadcast(
                kind, message, mission_id=mission_id, task_id=task_id, **payload
            )

        return hook

    # -- completion ------------------------------------------------------

    async def _finish(self, mission_id: str, tasks: list[Task]) -> None:
        succeeded = [t for t in tasks if t.status is TaskStatus.SUCCESS]
        failed = [t for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED)]

        summary = "\n\n".join(f"[{t.agent_id}] {t.result}" for t in succeeded if t.result)
        status = MissionStatus.COMPLETED if succeeded and not failed else (
            MissionStatus.FAILED if not succeeded else MissionStatus.COMPLETED
        )
        error = f"{len(failed)} task(s) did not succeed" if failed else ""

        store.update_mission(mission_id, status=status, summary=summary, error=error)
        await ws_manager.broadcast(
            "mission",
            f"Mission {status.value.lower()} ({len(succeeded)}/{len(tasks)} tasks)",
            mission_id=mission_id, status=status.value,
        )

    async def _abort(self, mission_id: str, tasks: list[Task], reason: str) -> None:
        for task in tasks:
            if not task.is_terminal:
                store.update_task(
                    task.task_id, status=TaskStatus.CANCELLED, error=reason, clear_lease=True
                )
        succeeded = [t for t in tasks if t.status is TaskStatus.SUCCESS]
        store.update_mission(
            mission_id,
            status=MissionStatus.FAILED,
            error=reason,
            summary="\n\n".join(f"[{t.agent_id}] {t.result}" for t in succeeded if t.result),
        )
        log.warning("mission %s aborted: %s", mission_id, reason)
        await ws_manager.broadcast(
            "mission", f"Mission aborted: {reason}", mission_id=mission_id, status="FAILED"
        )


scheduler = MissionScheduler()
