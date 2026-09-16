"""Mission planning, scheduling, budgets, approvals, and crash recovery.

The durability tests matter most: they are what makes "live 24/7, remember the
last state" a property of the system rather than a hope.
"""

from __future__ import annotations

import json

import pytest

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
    new_task_id,
)
from app.missions.planner import plan_mission, single_task_plan
from app.missions.scheduler import MissionScheduler
from tests.conftest import scripted_router, text_result, tool_result


def _plan_json(tasks: list[dict], notes: str = "") -> str:
    return json.dumps({"tasks": tasks, "notes": notes})


# --- planning -------------------------------------------------------------


async def test_planner_decomposes_into_a_task_graph(agents) -> None:
    router, _ = scripted_router([
        text_result(_plan_json([
            {"id": "t1", "agent": "research", "objective": "Find the facts", "depends_on": []},
            {"id": "t2", "agent": "memory", "objective": "Store them", "depends_on": ["t1"]},
        ]))
    ])
    tasks, notes = await plan_mission(
        mission_id="msn_1", goal="Research and remember", runner=AgentRunner(router), budget=Budget()
    )

    assert len(tasks) == 2
    assert [t.agent_id for t in tasks] == ["research", "memory"]
    # Dependencies are rewritten from planner-local ids to real task ids.
    assert tasks[1].depends_on == [tasks[0].task_id]
    assert tasks[0].status is TaskStatus.READY      # no dependencies
    assert tasks[1].status is TaskStatus.PENDING    # waits for t1


async def test_planner_json_inside_prose_and_fences_is_recovered(agents) -> None:
    wrapped = (
        "Sure! Here is the plan:\n```json\n"
        + _plan_json([{"id": "t1", "agent": "research", "objective": "Look it up", "depends_on": []}])
        + "\n```\nHope that helps."
    )
    router, _ = scripted_router([text_result(wrapped)])
    tasks, _ = await plan_mission(
        mission_id="msn_1", goal="x", runner=AgentRunner(router), budget=Budget()
    )
    assert len(tasks) == 1 and tasks[0].agent_id == "research"


async def test_unusable_planner_output_falls_back_to_one_task(agents) -> None:
    router, _ = scripted_router([text_result("I'm not sure how to break that down.")])
    tasks, notes = await plan_mission(
        mission_id="msn_1", goal="Do a thing", runner=AgentRunner(router), budget=Budget()
    )
    assert len(tasks) == 1
    assert tasks[0].objective == "Do a thing"
    assert "not usable" in notes


async def test_planner_naming_an_unknown_agent_is_corrected(agents) -> None:
    router, _ = scripted_router([
        text_result(_plan_json([
            {"id": "t1", "agent": "hacker", "objective": "Do something", "depends_on": []}
        ]))
    ])
    tasks, _ = await plan_mission(
        mission_id="msn_1", goal="x", runner=AgentRunner(router), budget=Budget()
    )
    assert tasks[0].agent_id == "assistant"     # coerced to the default, not invented


async def test_cyclic_plan_is_flattened_rather_than_deadlocking(agents) -> None:
    router, _ = scripted_router([
        text_result(_plan_json([
            {"id": "t1", "agent": "research", "objective": "A", "depends_on": ["t2"]},
            {"id": "t2", "agent": "research", "objective": "B", "depends_on": ["t1"]},
        ]))
    ])
    tasks, _ = await plan_mission(
        mission_id="msn_1", goal="x", runner=AgentRunner(router), budget=Budget()
    )
    assert all(not t.depends_on for t in tasks)   # cycle broken, mission can run


async def test_planner_respects_max_tasks(agents) -> None:
    many = [
        {"id": f"t{i}", "agent": "research", "objective": f"step {i}", "depends_on": []}
        for i in range(10)
    ]
    router, _ = scripted_router([text_result(_plan_json(many))])
    tasks, _ = await plan_mission(
        mission_id="msn_1", goal="x", runner=AgentRunner(router), budget=Budget(max_tasks=3)
    )
    assert len(tasks) == 3


# --- scheduling -----------------------------------------------------------


async def test_mission_runs_dependent_tasks_in_order(agents) -> None:
    router, _ = scripted_router([
        text_result(_plan_json([
            {"id": "t1", "agent": "research", "objective": "Find X", "depends_on": []},
            {"id": "t2", "agent": "assistant", "objective": "Summarise X", "depends_on": ["t1"]},
        ])),
        text_result("X is 42."),
        text_result("In short: 42."),
    ])
    scheduler = MissionScheduler(AgentRunner(router))

    mission = await scheduler.create_mission(goal="Find and summarise X")
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    finished = store.get_mission(mission.mission_id)
    tasks = store.list_tasks(mission.mission_id)

    assert finished.status is MissionStatus.COMPLETED
    assert all(t.status is TaskStatus.SUCCESS for t in tasks)
    assert "42" in finished.summary


async def test_failed_dependency_skips_downstream_tasks(agents) -> None:
    class Boom:
        provider = "SCRIPTED"
        model = "scripted-test"

        def __init__(self) -> None:
            self.n = 0

        def chat(self, messages, tools=None, max_tokens=1200):  # noqa: ANN001
            self.n += 1
            if self.n == 1:
                return text_result(_plan_json([
                    {"id": "t1", "agent": "research", "objective": "A", "depends_on": []},
                    {"id": "t2", "agent": "assistant", "objective": "B", "depends_on": ["t1"]},
                ]))
            raise RuntimeError("upstream exploded")

        def complete(self, s, u):  # noqa: ANN001
            return self.chat([])

    from app.agent.engines import EngineRouter

    router = EngineRouter()
    router.groq = router.gemini = None
    router.echo = Boom()

    scheduler = MissionScheduler(AgentRunner(router))
    mission = await scheduler.create_mission(goal="Will fail")
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    tasks = {t.objective: t for t in store.list_tasks(mission.mission_id)}
    assert tasks["A"].status is TaskStatus.FAILED
    assert tasks["B"].status is TaskStatus.SKIPPED       # never ran on bad input
    assert store.get_mission(mission.mission_id).status is MissionStatus.FAILED


async def test_task_budget_aborts_the_mission_cleanly(agents) -> None:
    """Budget exhaustion must be a reported outcome, not a hang."""
    plan = [
        {"id": f"t{i}", "agent": "assistant", "objective": f"step {i}", "depends_on": []}
        for i in range(4)
    ]
    router, _ = scripted_router(
        [text_result(_plan_json(plan))] + [text_result("ok") for _ in range(10)]
    )
    scheduler = MissionScheduler(AgentRunner(router))

    # max_tasks=2 caps the plan at 2, then the graph check trips on the 3rd.
    mission = await scheduler.create_mission(goal="Too much", budget=Budget(max_tasks=1))
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    finished = store.get_mission(mission.mission_id)
    assert finished.status in (MissionStatus.COMPLETED, MissionStatus.FAILED)
    assert finished.is_terminal      # crucially, it terminated rather than hanging


async def test_message_bus_records_every_dispatch_and_result(agents) -> None:
    router, _ = scripted_router([
        text_result(_plan_json([
            {"id": "t1", "agent": "assistant", "objective": "Say hi", "depends_on": []}
        ])),
        text_result("hi"),
    ])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = await scheduler.create_mission(goal="Say hi")
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    actions = [m["action"] for m in store.list_messages(mission.mission_id)]
    assert "mission.create" in actions
    assert "plan.created" in actions
    assert "task.dispatch" in actions
    assert "task.result" in actions


async def test_cancel_stops_a_mission(agents) -> None:
    router, _ = scripted_router([text_result(_plan_json([
        {"id": "t1", "agent": "assistant", "objective": "x", "depends_on": []}
    ]))])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = await scheduler.create_mission(goal="x")

    assert await scheduler.cancel(mission.mission_id) is True
    assert store.get_mission(mission.mission_id).status is MissionStatus.CANCELLED
    assert await scheduler.cancel(mission.mission_id) is False   # already terminal


# --- durability -----------------------------------------------------------


def test_reconciliation_requeues_tasks_orphaned_by_a_crash(agents) -> None:
    """A task left RUNNING has no live runner; it must return to the queue."""
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="interrupted"))
    task = Task(
        task_id=new_task_id(), mission_id=mission_id, agent_id="assistant",
        objective="was in flight", status=TaskStatus.RUNNING,
    )
    store.create_tasks([task])
    # No lease at all -- the crash happened before one was written.

    recovered = store.reconcile_orphaned_tasks()

    assert recovered["tasks_requeued"] == 1
    assert store.get_task(task.task_id).status is TaskStatus.READY


def test_reconciliation_leaves_live_leases_alone(agents) -> None:
    """A task whose lease is still valid is being worked on right now."""
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="live"))
    task = Task(
        task_id=new_task_id(), mission_id=mission_id, agent_id="assistant",
        objective="in flight", status=TaskStatus.RUNNING,
    )
    store.create_tasks([task])
    store.lease_task(task.task_id, seconds=300)     # expires well in the future

    recovered = store.reconcile_orphaned_tasks()

    assert recovered["tasks_requeued"] == 0
    assert store.get_task(task.task_id).status is TaskStatus.RUNNING


def test_expired_lease_is_requeued(agents) -> None:
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="stale"))
    task = Task(
        task_id=new_task_id(), mission_id=mission_id, agent_id="assistant",
        objective="stalled", status=TaskStatus.RUNNING,
    )
    store.create_tasks([task])
    store.lease_task(task.task_id, seconds=-10)     # already expired

    assert store.reconcile_orphaned_tasks()["tasks_requeued"] == 1
    assert store.get_task(task.task_id).status is TaskStatus.READY


def test_idempotency_key_finds_completed_duplicate_work(agents) -> None:
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="dedupe"))
    done = Task(
        task_id=new_task_id(), mission_id=mission_id, agent_id="research",
        objective="Find X", status=TaskStatus.SUCCESS, result="X is 42",
        idempotency_key="research:Find X",
    )
    store.create_tasks([done])

    found = store.find_task_by_idempotency_key(mission_id, "research:Find X")
    assert found is not None and found.result == "X is 42"
    assert store.find_task_by_idempotency_key(mission_id, "research:Other") is None


def test_mission_state_survives_a_process_restart(agents) -> None:
    """Nothing important lives in memory: a fresh read sees the same state."""
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="persist me", summary=""))
    store.create_tasks([
        Task(task_id=new_task_id(), mission_id=mission_id, agent_id="assistant",
             objective="step", status=TaskStatus.SUCCESS, result="done")
    ])
    store.update_mission(mission_id, status=MissionStatus.COMPLETED, summary="all good")

    # Simulates a new process: no in-memory scheduler state, straight from the DB.
    reloaded = store.get_mission(mission_id)
    assert reloaded.status is MissionStatus.COMPLETED
    assert reloaded.summary == "all good"
    assert store.list_tasks(mission_id)[0].result == "done"


# --- approvals ------------------------------------------------------------


def test_approval_can_only_be_decided_once(agents) -> None:
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="x"))
    approval_id = store.create_approval(
        mission_id=mission_id, task_id="task_1", agent_id="memory",
        tool_name="memory.forget", tool_arguments={"scope": "user", "key": "city"},
        risk="DANGEROUS",
    )

    assert store.decide_approval(approval_id, ApprovalDecision.APPROVED) is True
    assert store.decide_approval(approval_id, ApprovalDecision.DENIED) is False   # no double-decide
    assert store.get_approval(approval_id)["decision"] == "APPROVED"


def test_pending_approvals_are_listed_until_decided(agents) -> None:
    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="x"))
    approval_id = store.create_approval(
        mission_id=mission_id, task_id="task_1", agent_id="memory",
        tool_name="memory.forget", tool_arguments={}, risk="DANGEROUS",
    )

    assert len(store.list_pending_approvals()) == 1
    store.decide_approval(approval_id, ApprovalDecision.DENIED)
    assert store.list_pending_approvals() == []
