"""Analyst agents: read-only tools, summary contract, scheduler dispatch.

Fixtures are the trading-v1 examples. The model is scripted. No engine is dialed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.models import Outcome
from app.adapters.trading.base import TradingRead
from app.agents.analysts import ANALYST_AGENT_IDS
from app.agents.runner import AgentRunner
from app.agents.trading_summary import evidence_from_output, validate_trading_summary
from app.contracts.trading_v1.models import PositionResponse, SignalResponse
from app.missions import store
from app.missions.models import Budget, MissionStatus, Task, TaskStatus, new_task_id
from app.missions.planner import plan_mission
from app.missions.scheduler import MissionScheduler
from app.tools.cloud.trading import set_trading_adapter, trading_crypto_read, trading_nse_read
from app.tools.registry import Capability, Risk, Tool, check_permission, is_order_like
from tests.conftest import scripted_router, text_result, tool_result

FIXTURES = Path("docs/contracts/trading-v1/fixtures")


class ScriptFeed:
    def __init__(self, **reads: TradingRead) -> None:
        self.reads = reads
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name not in self.reads:
            raise AttributeError(name)

        async def call(**_kwargs):
            self.calls.append(name)
            return self.reads[name]

        return call


@pytest.fixture(autouse=True)
def _clear_feeds():
    yield
    set_trading_adapter("crypto", None)
    set_trading_adapter("nse", None)


def _fixture(engine: str, name: str) -> dict:
    return json.loads((FIXTURES / engine / name).read_text(encoding="utf-8"))


def _read(model, payload: dict) -> TradingRead:
    envelope = model.model_validate(payload)
    stale = bool(envelope.is_stale)
    return TradingRead(
        adapter=envelope.source_engine.value,
        outcome=Outcome.STALE if stale else Outcome.OK,
        stale=stale,
        correlation_id=envelope.correlation_id,
        envelope=envelope,
    )


def _down(source: str) -> TradingRead:
    return TradingRead(
        adapter=source,
        outcome=Outcome.UNAVAILABLE,
        stale=False,
        correlation_id="",
        envelope=None,
        reason="http_5xx",
        detail=f"{source} returned HTTP 500",
    )


def _summary(evidence, holdings: list[str], *, announce: str | None = None) -> str:
    lines: list[str] = []
    if announce == "stale":
        bits = [
            f"{item.source_engine} data_as_of {item.data_as_of}"
            for item in evidence
            if item.stale
        ]
        lines.append("STALE: " + "; ".join(bits))
    elif announce == "down":
        bits = [
            f"{item.source_engine} data_as_of {item.data_as_of}"
            for item in evidence
            if item.unavailable
        ]
        lines.append("UNAVAILABLE: " + "; ".join(bits))
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        pair = (item.source_engine, item.data_as_of)
        if pair in seen:
            continue
        seen.add(pair)
        lines.append(f"SOURCE: {item.source_engine}")
        lines.append(f"DATA_AS_OF: {item.data_as_of}")
    lines.append("HOLDINGS: " + (",".join(holdings) if holdings else "none"))
    lines.append("")
    lines.append("Figures are copied from the adapter.")
    lines.append("")
    lines.append("## Assumptions")
    lines.append("The adapter response is the only source for these figures.")
    lines.append("")
    lines.append("## Uncertainty")
    lines.append("A single snapshot can be wrong by the time it is read.")
    return "\n".join(lines)


async def _crypto_output(resource: str, read: TradingRead) -> tuple[str, object]:
    set_trading_adapter("crypto", ScriptFeed(**{resource: read}))
    output = await trading_crypto_read(resource=resource)
    evidence = evidence_from_output(output)
    assert evidence is not None
    return output, evidence


def test_analysts_are_read_only_and_registered(agents) -> None:
    for agent_id in ANALYST_AGENT_IDS:
        definition = agents.get(agent_id)
        assert definition is not None
        assert definition.output_contract == "trading_summary"
        assert definition.capabilities == {Capability.READ}
        assert all(name.startswith("trading.") for name in definition.tools)
        assert "memory.write" not in definition.tools
        assert "shell.exec" not in definition.tools
    assert "crypto_analyst:" in agents.roster_for_planner()
    crypto = tool_registry_get("trading.crypto.read")
    nse = tool_registry_get("trading.nse.read")
    assert crypto.capability is Capability.READ and crypto.risk is Risk.SAFE
    assert nse.capability is Capability.READ and nse.risk is Risk.SAFE


def tool_registry_get(name: str):
    from app.tools.registry import registry as tool_registry

    tool = tool_registry.get(name)
    assert tool is not None
    return tool


def test_order_like_tools_are_denied_even_when_allowlisted() -> None:
    async def _handler(**_kwargs: object) -> str:
        return "no"

    for name in ("broker.place_order", "broker.cancel", "account.withdraw", "trading.buy"):
        assert is_order_like(name)
        verdict = check_permission(
            Tool(
                name=name,
                description="should never run",
                parameters={"type": "object", "properties": {}},
                capability=Capability.READ,
                risk=Risk.DANGEROUS,
                handler=_handler,
            ),
            granted_capabilities={Capability.READ, Capability.FINANCIAL},
            allowed_tools=[name],
        )
        assert verdict.allowed is False
        assert verdict.needs_approval is False
        assert verdict.reason == "order-like tools are denied"
    assert is_order_like("trading.crypto.read") is False
    assert is_order_like("memory.write") is False


async def test_planner_keeps_an_analyst_assignment(agents) -> None:
    router, _ = scripted_router([
        text_result(json.dumps({
            "tasks": [{
                "id": "t1",
                "agent": "crypto_analyst",
                "objective": "Read the crypto book",
                "depends_on": [],
            }],
            "notes": "",
        }))
    ])
    tasks, _notes = await plan_mission(
        mission_id="msn_analyst",
        goal="Read the crypto book",
        runner=AgentRunner(router),
        budget=Budget(),
    )
    assert tasks[0].agent_id == "crypto_analyst"


async def test_healthy_signal_fixture_summary(agents) -> None:
    _output, evidence = await _crypto_output("signals", _read(SignalResponse, _fixture("crypto", "signals.json")))
    text = _summary([evidence], [])
    assert validate_trading_summary(text, [evidence]) == []
    assert "crypto-signal-engine" in text
    assert evidence.data_as_of in text
    # The signal names BTCUSDT. That is not a holding without a positions read.
    invented = text.replace("HOLDINGS: none", "HOLDINGS: BTCUSDT")
    problems = validate_trading_summary(invented, [evidence])
    assert any("invented holding" in problem for problem in problems)


async def test_stale_position_fixture_must_say_so_first(agents) -> None:
    _output, evidence = await _crypto_output(
        "positions", _read(PositionResponse, _fixture("crypto", "positions.json"))
    )
    assert evidence.stale is True
    assert "BTCUSDT" in evidence.holdings
    good = _summary([evidence], ["BTCUSDT"], announce="stale")
    assert validate_trading_summary(good, [evidence]) == []
    assert good.startswith("STALE:")
    quiet = _summary([evidence], ["BTCUSDT"])
    problems = validate_trading_summary(quiet, [evidence])
    assert any("first line" in problem for problem in problems)


async def test_down_feed_says_unavailable_first_and_invents_nothing(agents) -> None:
    _output, evidence = await _crypto_output("positions", _down("crypto-signal-engine"))
    assert evidence.unavailable is True
    assert evidence.data_as_of == "unknown"
    assert evidence.holdings == frozenset()
    good = _summary([evidence], [], announce="down")
    assert validate_trading_summary(good, [evidence]) == []
    assert good.startswith("UNAVAILABLE:")
    invented = _summary([evidence], ["BTCUSDT"], announce="down")
    problems = validate_trading_summary(invented, [evidence])
    assert any("invented holding" in problem and "BTCUSDT" in problem for problem in problems)


async def test_nse_position_fixture_accepts_only_the_reported_symbol(agents) -> None:
    payload = _fixture("nse", "positions.json")
    set_trading_adapter("nse", ScriptFeed(positions=_read(PositionResponse, payload)))
    output = await trading_nse_read(resource="positions")
    evidence = evidence_from_output(output)
    assert evidence is not None and evidence.stale is False
    assert validate_trading_summary(_summary([evidence], ["RELIANCE"]), [evidence]) == []
    problems = validate_trading_summary(_summary([evidence], ["DOGEUSDT"]), [evidence])
    assert any("invented holding" in problem and "DOGEUSDT" in problem for problem in problems)


async def test_runner_retries_an_invented_holding_then_accepts(agents) -> None:
    read = _read(PositionResponse, _fixture("crypto", "positions.json"))
    set_trading_adapter("crypto", ScriptFeed(positions=read))
    preview = await trading_crypto_read(resource="positions")
    evidence = evidence_from_output(preview)
    assert evidence is not None
    bad = _summary([evidence], ["DOGEUSDT"], announce="stale")
    good = _summary([evidence], ["BTCUSDT"], announce="stale")
    router, engine = scripted_router([
        tool_result("trading.crypto.read", {"resource": "positions"}),
        text_result(bad),
        text_result(good),
    ])

    result = await AgentRunner(router).run(
        agents.get("crypto_analyst"), objective="What is the spot book?"
    )

    assert result.accepted is True
    assert "DOGEUSDT" not in result.text
    assert "BTCUSDT" in result.text
    assert "crypto-signal-engine" in result.text
    assert evidence.data_as_of in result.text
    assert "## Assumptions" in result.text
    assert "## Uncertainty" in result.text
    assert result.text.startswith("STALE:")
    # The rejected draft was shown back to the model, not stored.
    assert any("invented holding" in message["content"] for message in engine.calls[-1]["messages"])


async def test_exhausted_retries_do_not_store_the_invented_holding(agents) -> None:
    read = _read(PositionResponse, _fixture("crypto", "positions.json"))
    set_trading_adapter("crypto", ScriptFeed(positions=read))
    preview = await trading_crypto_read(resource="positions")
    evidence = evidence_from_output(preview)
    bad = _summary([evidence], ["DOGEUSDT"], announce="stale")
    router, _engine = scripted_router([
        tool_result("trading.crypto.read", {"resource": "positions"}),
        text_result(bad),
        text_result(bad),
        text_result(bad),
    ])

    result = await AgentRunner(router).run(
        agents.get("crypto_analyst"), objective="What is the spot book?"
    )

    assert result.accepted is False
    assert result.text.startswith("[summary rejected]")
    assert "HOLDINGS:" not in result.text
    assert "invented holding rejected" in result.text


async def test_scheduler_dispatches_crypto_analyst_on_a_healthy_path(agents) -> None:
    """Registration is the YAML id the scheduler already dispatches by."""
    payload = _fixture("crypto", "signals.json")
    read = _read(SignalResponse, payload)
    set_trading_adapter("crypto", ScriptFeed(signals=read))
    preview = await trading_crypto_read(resource="signals")
    evidence = evidence_from_output(preview)
    good = _summary([evidence], [])
    router, _engine = scripted_router([
        tool_result("trading.crypto.read", {"resource": "signals"}),
        text_result(good),
    ])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = await scheduler.create_mission(goal="Read crypto signals")
    store.create_tasks([
        Task(
            task_id=new_task_id(),
            mission_id=mission.mission_id,
            agent_id="crypto_analyst",
            objective="Summarise the latest crypto signals",
            status=TaskStatus.READY,
            idempotency_key="crypto-signals",
        )
    ])
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    finished = store.get_mission(mission.mission_id)
    task = store.list_tasks(mission.mission_id)[0]
    assert task.status is TaskStatus.SUCCESS
    assert task.agent_id == "crypto_analyst"
    assert "crypto-signal-engine" in task.result
    assert evidence.data_as_of in task.result
    assert "## Uncertainty" in task.result
    assert finished.status is MissionStatus.COMPLETED
    receivers = [item["receiver"] for item in store.list_messages(mission.mission_id)]
    assert "crypto_analyst" in receivers


async def test_scheduler_drops_a_rejected_holding(agents) -> None:
    read = _read(PositionResponse, _fixture("crypto", "positions.json"))
    set_trading_adapter("crypto", ScriptFeed(positions=read))
    preview = await trading_crypto_read(resource="positions")
    evidence = evidence_from_output(preview)
    bad = _summary([evidence], ["DOGEUSDT"], announce="stale")
    router, _engine = scripted_router([
        tool_result("trading.crypto.read", {"resource": "positions"}),
        text_result(bad),
        text_result(bad),
        text_result(bad),
    ])
    scheduler = MissionScheduler(AgentRunner(router))
    mission = await scheduler.create_mission(goal="Read the book")
    store.create_tasks([
        Task(
            task_id=new_task_id(),
            mission_id=mission.mission_id,
            agent_id="portfolio_analyst",
            objective="List holdings",
            status=TaskStatus.READY,
            idempotency_key="book",
        )
    ])
    scheduler.start(mission.mission_id)
    await scheduler.wait_for(mission.mission_id, timeout=20)

    task = store.list_tasks(mission.mission_id)[0]
    finished = store.get_mission(mission.mission_id)
    assert task.status is TaskStatus.FAILED
    assert task.result == ""
    assert "invented holding rejected" in task.error
    assert "DOGEUSDT" not in (finished.summary or "")
    assert finished.status is MissionStatus.FAILED


async def test_crypto_analyst_cannot_call_the_nse_tool(agents) -> None:
    router, _engine = scripted_router([
        tool_result("trading.nse.read", {"resource": "positions"}),
        text_result("no data"),
        text_result("no data"),
        text_result("no data"),
    ])
    result = await AgentRunner(router).run(
        agents.get("crypto_analyst"), objective="Read NSE anyway"
    )
    assert "[denied:" in result.tool_log[0]["output"]
    assert result.accepted is False


async def test_missing_base_url_is_unavailable_and_does_not_invent(agents, monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_ENGINE_BASE_URL", raising=False)
    output = await trading_crypto_read(resource="positions")
    evidence = evidence_from_output(output)
    assert evidence is not None
    assert evidence.unavailable is True
    assert "CRYPTO_ENGINE_BASE_URL is not set" in output
    assert "BTCUSDT" not in output.split("---", 1)[0]
