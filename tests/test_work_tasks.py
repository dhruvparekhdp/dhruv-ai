"""Personal tasks: CRUD, IST recurrence, and the suggestion gate.

Due times in the assertions are Asia/Kolkata wall clocks.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agents.runner import AgentRunner
from app.missions.models import Mission, new_mission_id
from app.missions import store as mission_store
from app.tools.cloud.tasks import tasks_suggest
from app.tools.registry import Capability, Risk, registry as tool_registry
from app.work_tasks.models import Actor, TaskError, TaskStatus
from app.work_tasks import store
from tests.conftest import scripted_router, text_result, tool_result

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def tasks(env):
    store.init_db()
    mission_store.init_db()
    return store


def test_crud_round_trip(tasks) -> None:
    created = tasks.create(
        "File GST",
        actor=Actor.USER,
        notes="the portal one",
        due_at="2026-11-06T18:30:00",
        priority=1,
        tags=["Tax", "tax", "finance"],
        recurrence="none",
    )
    assert created.status is TaskStatus.OPEN
    assert created.source is Actor.USER
    assert created.tags == ("finance", "tax")
    assert created.to_dict()["due_at_ist"].startswith("2026-11-06T18:30:00+05:30")
    assert created.to_dict()["due_at"].startswith("2026-11-06T13:00:00")

    same_instant = tasks.create("utc form", actor="user", due_at="2026-11-06T13:00:00Z")
    assert same_instant.to_dict()["due_at_ist"].startswith("2026-11-06T18:30:00+05:30")

    date_only = tasks.create("morning", actor="user", due_at="2026-11-07")
    assert date_only.to_dict()["due_at_ist"].startswith("2026-11-07T00:00:00+05:30")

    renamed = tasks.update(created.task_id, title="File GST return", priority=2)
    assert renamed is not None
    assert renamed.title == "File GST return"
    assert renamed.priority == 2

    assert tasks.get("wtsk_missing") is None
    assert tasks.delete(created.task_id) is True
    assert tasks.get(created.task_id) is None
    assert tasks.delete(created.task_id) is False


def test_links_check_missions_and_leave_brands_unchecked(tasks) -> None:
    with pytest.raises(TaskError, match="unknown mission"):
        tasks.create("linked", actor="user", link_kind="mission", link_id="msn_missing")

    mission = Mission(mission_id=new_mission_id(), goal="ship the note")
    mission_store.create_mission(mission)
    linked = tasks.create(
        "linked", actor="user", link_kind="mission", link_id=mission.mission_id,
    )
    assert linked.to_dict()["link"] == {"kind": "mission", "id": mission.mission_id}

    brand = tasks.create("post", actor="user", link_kind="brand", link_id="northwind")
    assert brand.link_kind == "brand"
    content = tasks.create("draft", actor="user", link_kind="content_item", link_id="post-14")
    assert content.link_id == "post-14"
    with pytest.raises(TaskError, match="both"):
        tasks.create("half", actor="user", link_kind="brand")


def test_recurrence_advances_in_ist_and_clamps_the_month(tasks) -> None:
    daily = tasks.create(
        "standup", actor="user", due_at="2026-11-06T18:30:00", recurrence="daily",
    )
    done, nxt = tasks.complete(daily.task_id)
    assert done.status is TaskStatus.DONE
    assert nxt is not None
    assert nxt.status is TaskStatus.OPEN
    assert nxt.series_id == daily.series_id
    assert nxt.to_dict()["due_at_ist"].startswith("2026-11-07T18:30:00+05:30")

    weekly = tasks.create(
        "review", actor="user", due_at="2026-11-02T09:00:00", recurrence="weekly",
    )
    _, week = tasks.complete(weekly.task_id)
    assert week is not None
    assert week.to_dict()["due_at_ist"].startswith("2026-11-09T09:00:00+05:30")

    monthly = tasks.create(
        "rent", actor="user", due_at="2026-01-31T09:00:00", recurrence="monthly",
    )
    _, february = tasks.complete(monthly.task_id)
    assert february is not None
    assert february.to_dict()["due_at_ist"].startswith("2026-02-28T09:00:00+05:30")
    _, march = tasks.complete(february.task_id)
    assert march is not None
    assert march.to_dict()["due_at_ist"].startswith("2026-03-28T09:00:00+05:30")

    once = tasks.create("one-off", actor="user")
    finished, nothing = tasks.complete(once.task_id)
    assert finished.status is TaskStatus.DONE
    assert nothing is None


def test_recurrence_without_a_due_uses_the_completion_instant(tasks) -> None:
    task = tasks.create("daily note", actor="user", recurrence="daily")
    moment = datetime(2026, 11, 6, 18, 0, tzinfo=IST)
    _, nxt = tasks.complete(task.task_id, now=moment)
    assert nxt is not None
    assert nxt.to_dict()["due_at_ist"].startswith("2026-11-07T18:00:00+05:30")


def test_completing_does_not_file_a_duplicate_next_occurrence(tasks) -> None:
    first = tasks.create(
        "standup", actor="user", due_at="2026-11-06T18:30:00", recurrence="daily",
    )
    tasks.create(
        "already filed",
        actor="user",
        due_at="2026-11-07T18:30:00",
        recurrence="daily",
        series_id=first.series_id,
    )
    _, nxt = tasks.complete(first.task_id)
    assert nxt is not None
    assert nxt.title == "already filed"
    opens = [
        item for item in tasks.list_tasks()
        if item.series_id == first.series_id and item.status is TaskStatus.OPEN
    ]
    assert len(opens) == 1


def test_agents_may_only_suggest_and_the_user_accepts(tasks) -> None:
    with pytest.raises(TaskError, match="suggested"):
        tasks.create("skip the gate", actor=Actor.AGENT, status="open")
    with pytest.raises(TaskError, match="agents"):
        tasks.create("from me", actor=Actor.USER, status="suggested")

    suggested = tasks.create("Call the CA", actor=Actor.AGENT, tags=["tax"])
    assert suggested.status is TaskStatus.SUGGESTED
    assert tasks.list_tasks()[0].task_id == suggested.task_id

    with pytest.raises(TaskError, match="accept"):
        tasks.complete(suggested.task_id)
    with pytest.raises(TaskError, match="suggested"):
        tasks.accept(tasks.create("plain", actor="user").task_id)

    accepted = tasks.accept(suggested.task_id)
    assert accepted is not None
    assert accepted.status is TaskStatus.OPEN
    done, _ = tasks.complete(accepted.task_id)
    assert done.status is TaskStatus.DONE
    with pytest.raises(TaskError, match="frozen"):
        tasks.update(done.task_id, title="too late")
    with pytest.raises(TaskError, match="open"):
        tasks.complete(done.task_id)


def test_suggest_tool_drops_a_requested_status(tasks) -> None:
    import asyncio

    text = asyncio.run(tasks_suggest("Pack the bag", status="done", priority=1))
    assert "status suggested" in text
    found = tasks.list_tasks(status="suggested")
    assert len(found) == 1
    assert found[0].title == "Pack the bag"
    assert found[0].status is TaskStatus.SUGGESTED
    tool = tool_registry.get("tasks.suggest")
    assert tool is not None
    assert tool.capability is Capability.WRITE
    assert tool.risk is Risk.SAFE


async def test_assistant_suggestion_is_not_an_open_task(tasks, agents) -> None:
    router, _ = scripted_router([
        tool_result("tasks.suggest", {
            "title": "File the GST return",
            "due_at": "2026-11-07T18:30:00",
            "status": "open",
        }),
        text_result("Suggested, not accepted."),
    ])
    result = await AgentRunner(router).run(agents.get("assistant"), objective="Remind me about GST.")
    assert "suggested" in result.tool_log[0]["output"]
    rows = tasks.list_tasks(include_done=True)
    assert len(rows) == 1
    assert rows[0].status is TaskStatus.SUGGESTED
    assert rows[0].to_dict()["due_at_ist"].startswith("2026-11-07T18:30:00+05:30")
    assert "tasks.suggest" in agents.get("assistant").tools


def test_api_crud_accept_and_complete(client) -> None:
    denied = client.get("/api/v1/tasks", headers={"X-Jarvis-Passcode": "wrong"})
    assert denied.status_code == 401

    created = client.post("/api/v1/tasks", json={
        "title": "Write the note",
        "due_at": "2026-11-08T21:00:00",
        "tags": ["writing"],
        "recurrence": "weekly",
        "priority": 2,
    })
    assert created.status_code == 201
    body = created.json()["task"]
    assert body["status"] == "open"
    assert body["due_at_ist"].startswith("2026-11-08T21:00:00+05:30")
    task_id = body["task_id"]

    patched = client.patch(f"/api/v1/tasks/{task_id}", json={"notes": "keep it short"})
    assert patched.status_code == 200
    assert patched.json()["task"]["notes"] == "keep it short"

    listed = client.get("/api/v1/tasks?tag=writing").json()
    assert listed["count"] == 1

    missing = client.post("/api/v1/tasks/wtsk_missing/accept")
    assert missing.status_code == 404

    completed = client.post(f"/api/v1/tasks/{task_id}/complete").json()
    assert completed["task"]["status"] == "done"
    assert completed["next_task"]["status"] == "open"
    assert completed["next_task"]["due_at_ist"].startswith("2026-11-15T21:00:00+05:30")
    assert completed["next_task"]["series_id"] == body["series_id"]

    hidden = client.get("/api/v1/tasks").json()
    assert hidden["count"] == 1
    assert hidden["tasks"][0]["status"] == "open"
    shown = client.get("/api/v1/tasks?include_done=true").json()
    assert shown["count"] == 2

    removed = client.delete(f"/api/v1/tasks/{completed['next_task']['task_id']}")
    assert removed.status_code == 200
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200
