"""LLM usage ledger and spend budgets, with a mocked provider.

The scripted engine stands in for Groq or Gemini: no key, no network. Prices
are set absurdly high so a handful of tokens crosses a tiny budget on purpose.
"""

from __future__ import annotations

import asyncio
import json

from app.agents.runner import AgentRunner
from app.core import usage
from app.missions.models import MissionStatus
from app.missions.scheduler import MissionScheduler
from app.missions import store
from tests.conftest import scripted_router, text_result

_PRICE = json.dumps(
    {
        "SCRIPTED/scripted-test": {
            "input_per_million": 1_000_000,
            "output_per_million": 1_000_000,
        }
    }
)

_PLAN = json.dumps(
    {"tasks": [{"id": "t1", "agent": "assistant", "objective": "Say hi", "depends_on": []}]}
)


def _budgets(monkeypatch, *, mission: str = "100", day: str = "100", agent: str = "100") -> None:
    monkeypatch.setenv("JARVIS_PRICE_TABLE", _PRICE)
    monkeypatch.setenv("JARVIS_BUDGET_MISSION_USD", mission)
    monkeypatch.setenv("JARVIS_BUDGET_DAY_USD", day)
    monkeypatch.setenv("JARVIS_BUDGET_AGENT_USD", agent)


def test_mocked_provider_records_tokens_model_and_cost(agents, monkeypatch) -> None:
    _budgets(monkeypatch)
    router, _ = scripted_router([text_result("hi", prompt_tokens=20, completion_tokens=4)])

    with usage.scope("msn_test", "assistant"):
        router.chat(messages=[{"role": "user", "content": "hi"}])

    call = usage.report()["calls"][0]
    assert call["provider"] == "SCRIPTED"
    assert call["model"] == "scripted-test"
    assert call["prompt_tokens"] == 20
    assert call["completion_tokens"] == 4
    assert call["mission_id"] == "msn_test"
    assert call["agent_id"] == "assistant"
    assert call["cost_usd"] == "24.000000"
    assert call["price_known"] is True


def test_usage_api_breaks_down_day_agent_and_mission(agents, client, monkeypatch) -> None:
    _budgets(monkeypatch)
    router, _ = scripted_router([
        text_result(_PLAN, prompt_tokens=10, completion_tokens=5),
        text_result("hi", prompt_tokens=8, completion_tokens=3),
    ])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = _run(scheduler, "Say hi")

    assert store.get_mission(mission.mission_id).status is MissionStatus.COMPLETED

    body = client.get("/api/usage").json()
    assert body["status"] == "success"
    mission_row = next(row for row in body["by_mission"] if row["mission_id"] == mission.mission_id)
    assert mission_row["prompt_tokens"] == 18
    assert mission_row["completion_tokens"] == 8
    assert mission_row["calls"] == 2
    assert mission_row["cost_usd"] == "26.000000"

    by_agent = {row["agent_id"]: row for row in body["by_agent"]}
    assert by_agent["planner"]["prompt_tokens"] == 10
    assert by_agent["assistant"]["prompt_tokens"] == 8
    assert any(row["day"] == body["today"] and row["calls"] == 2 for row in body["daily"])

    providers = {row["provider"] for row in body["calls"]}
    models = {row["model"] for row in body["calls"]}
    assert providers == {"SCRIPTED"}
    assert models == {"scripted-test"}


def test_exceeding_the_mission_budget_pauses_for_approval(agents, monkeypatch) -> None:
    _budgets(monkeypatch, mission="0.01")
    router, _ = scripted_router([
        text_result(_PLAN, prompt_tokens=10, completion_tokens=5),
        text_result("should not run"),
    ])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = _run(scheduler, "Say hi")

    finished = store.get_mission(mission.mission_id)
    assert finished is not None
    assert finished.status is MissionStatus.NEEDS_APPROVAL
    assert finished.status is not MissionStatus.FAILED
    assert "mission budget exceeded" in finished.error

    pending = store.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "budget.exceeded"
    assert pending[0]["mission_id"] == mission.mission_id
    assert "mission budget exceeded" in pending[0]["tool_arguments"]["reason"]

    # The call that crossed the line is recorded. The next one was not made.
    calls = usage.report(mission_id=mission.mission_id)["calls"]
    assert len(calls) == 1
    assert calls[0]["provider"] == "SCRIPTED"
    assert calls[0]["prompt_tokens"] == 10
    assert calls[0]["completion_tokens"] == 5


def test_exceeding_the_daily_or_agent_budget_also_pauses(agents, monkeypatch) -> None:
    _budgets(monkeypatch, day="0.01", agent="0.01")
    router, _ = scripted_router([text_result(_PLAN, prompt_tokens=10, completion_tokens=5)])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = _run(scheduler, "Say hi")

    finished = store.get_mission(mission.mission_id)
    assert finished.status is MissionStatus.NEEDS_APPROVAL
    assert "daily budget exceeded" in finished.error
    assert store.list_pending_approvals()[0]["tool_name"] == "budget.exceeded"


def _run(scheduler: MissionScheduler, goal: str):
    async def _go():
        mission = await scheduler.create_mission(goal=goal)
        scheduler.start(mission.mission_id)
        await scheduler.wait_for(mission.mission_id, timeout=20)
        return mission

    return asyncio.run(_go())
