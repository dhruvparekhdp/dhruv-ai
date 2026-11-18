"""Alert firing, dedupe, quiet hours, and the daily briefing.

Times are Asia/Kolkata wall clocks. The machine zone is not consulted.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.alerts import briefing
from app.alerts.rules import (
    IST,
    AlertRules,
    EngineSnapshot,
    SignalFact,
    delivery_decision,
    evaluate,
    in_quiet_hours,
    load_rules,
)
from app.alerts.rules import Delivery
from app.alerts import store as alert_store
from app.analytics.outcomes import compute_outcomes
from app.missions import store as mission_store
from app.missions.models import TaskStatus

T_OPEN = "2026-11-01T00:00:00Z"


def _at(hour: int, minute: int, *, day: int = 6) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def _rules(**overrides) -> AlertRules:
    base = dict(
        signal_strength_above=Decimal("0.7"),
        stale_after=timedelta(minutes=15),
        drawdown_percent=Decimal("5"),
        dedupe_window=timedelta(minutes=60),
        quiet_start=time(22, 0),
        quiet_end=time(7, 0),
        briefing_at=time(8, 0),
    )
    base.update(overrides)
    return AlertRules(**base)


def _trade(trade_id: str, pnl: str, closed_at: str) -> dict:
    return {
        "id": trade_id,
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": "1",
        "price": {"amount": "1", "currency": "USDT"},
        "fees": {"amount": "0", "currency": "USDT"},
        "opened_at": T_OPEN,
        "closed_at": closed_at,
        "pnl": {"amount": pnl, "currency": "USDT"},
        "strategy": "momentum",
        "mode": "paper",
    }


def _snapshot(**overrides) -> EngineSnapshot:
    base = dict(
        source_engine="crypto-signal-engine",
        data_as_of=_at(8, 0).astimezone(ZoneInfo("UTC")),
        signals=(),
        trades=(),
        breaches=(),
    )
    base.update(overrides)
    return EngineSnapshot(**base)


@pytest.fixture()
def inbox(env):
    alert_store.init_db()
    mission_store.init_db()
    return alert_store


def test_signal_fires_only_above_the_threshold() -> None:
    rules = _rules()
    now = _at(8, 0)
    as_of = now
    below = evaluate(
        _snapshot(
            data_as_of=as_of,
            signals=(
                SignalFact("s-eq", "BTCUSDT", Decimal("0.7"), "active"),
                SignalFact("s-low", "ETHUSDT", Decimal("0.69"), "active"),
                SignalFact("s-closed", "SOLUSDT", Decimal("0.99"), "closed"),
            ),
        ),
        rules,
        now,
    )
    assert below == []

    above = evaluate(
        _snapshot(
            data_as_of=as_of,
            signals=(SignalFact("s-hi", "BTCUSDT", Decimal("0.71"), "active"),),
        ),
        rules,
        now,
    )
    assert len(above) == 1
    assert above[0].rule == "signal"
    assert above[0].severity == "warning"
    assert above[0].dedupe_key == "signal:crypto-signal-engine:s-hi"
    assert "BTCUSDT" in above[0].body


def test_stale_fires_only_when_age_is_past_n_minutes() -> None:
    rules = _rules()
    now = _at(9, 0)
    exact = evaluate(_snapshot(data_as_of=now - timedelta(minutes=15)), rules, now)
    younger = evaluate(_snapshot(data_as_of=now - timedelta(minutes=14)), rules, now)
    missing = evaluate(_snapshot(data_as_of=None), rules, now)
    assert exact == []
    assert younger == []
    assert missing == []

    older = evaluate(_snapshot(data_as_of=now - timedelta(minutes=15, seconds=1)), rules, now)
    assert len(older) == 1
    assert older[0].rule == "stale"
    assert older[0].severity == "warning"
    assert older[0].dedupe_key == "stale:crypto-signal-engine"


def test_drawdown_percent_fires_only_above_y() -> None:
    # +10 then -4. Peak 10, trough 6, percent 0.4.
    book = (
        _trade("a", "10", "2026-11-01T00:00:00Z"),
        _trade("b", "-4", "2026-11-02T00:00:00Z"),
    )
    report = compute_outcomes(book).reports[0]
    assert report.max_drawdown_pct == Decimal("0.4")

    now = _at(8, 0)
    at_limit = evaluate(_snapshot(trades=book), _rules(drawdown_percent=Decimal("40")), now)
    under = evaluate(_snapshot(trades=book), _rules(drawdown_percent=Decimal("50")), now)
    assert at_limit == []
    assert under == []

    over = evaluate(_snapshot(trades=book), _rules(drawdown_percent=Decimal("39")), now)
    assert len(over) == 1
    assert over[0].rule == "drawdown"
    assert over[0].severity == "critical"
    assert over[0].dedupe_key == "drawdown:paper:USDT"

    # Never above zero: no percent, so the rule stays quiet.
    losses = (_trade("c", "-5", "2026-11-03T00:00:00Z"),)
    assert compute_outcomes(losses).reports[0].max_drawdown_pct is None
    assert evaluate(_snapshot(trades=losses), _rules(drawdown_percent=Decimal("1")), now) == []


def test_risk_breach_fires_once_per_name() -> None:
    now = _at(8, 0)
    assert evaluate(_snapshot(breaches=()), _rules(), now) == []
    drafts = evaluate(
        _snapshot(breaches=("book_notional", "  ", "book_notional")),
        _rules(),
        now,
    )
    assert len(drafts) == 1
    assert drafts[0].rule == "risk"
    assert drafts[0].severity == "critical"
    assert drafts[0].dedupe_key == "risk:crypto-signal-engine:book_notional"


@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [
        (22, 0, True),
        (21, 59, False),
        (23, 30, True),
        (0, 30, True),
        (7, 0, False),
        (8, 0, False),
    ],
)
def test_quiet_hours_are_ist_half_open(hour: int, minute: int, quiet: bool) -> None:
    start = time(22, 0)
    end = time(7, 0)
    assert in_quiet_hours(_at(hour, minute), start, end) is quiet


def test_quiet_hours_suppress_and_do_not_start_dedupe(inbox) -> None:
    rules = _rules()
    now = _at(23, 30)
    drafts = evaluate(
        _snapshot(
            data_as_of=now,
            signals=(SignalFact("s1", "BTCUSDT", Decimal("0.9"), "active"),),
        ),
        rules,
        now,
    )
    filed = inbox.file_alerts(drafts, rules, now)
    assert filed[0]["delivery"] == "suppressed"
    assert filed[0]["status"] == "suppressed"
    assert inbox.inbox() == []

    later = _at(8, 0, day=7)
    again = inbox.file_alerts(drafts, rules, later)
    assert again[0]["delivery"] == "deliver"
    assert len(inbox.inbox()) == 1


def test_dedupe_window_blocks_a_second_delivery_until_it_ends(inbox) -> None:
    rules = _rules()
    first_at = _at(8, 0)
    drafts = evaluate(
        _snapshot(signals=(SignalFact("s1", "BTCUSDT", Decimal("0.9"), "active"),)),
        rules,
        first_at,
    )
    assert inbox.file_alerts(drafts, rules, first_at)[0]["delivery"] == "deliver"
    assert len(inbox.inbox()) == 1

    still = inbox.file_alerts(drafts, rules, _at(9, 0))
    assert still[0]["delivery"] == "duplicate"
    assert "alert_id" not in still[0]
    assert len(inbox.inbox()) == 1

    after = inbox.file_alerts(drafts, rules, _at(9, 1))
    assert after[0]["delivery"] == "deliver"
    assert len(inbox.inbox()) == 2


def test_same_batch_does_not_deliver_one_key_twice(inbox) -> None:
    rules = _rules()
    now = _at(8, 0)
    draft = evaluate(
        _snapshot(signals=(SignalFact("s1", "BTCUSDT", Decimal("0.9"), "active"),)),
        rules,
        now,
    )[0]
    filed = inbox.file_alerts([draft, draft], rules, now)
    assert [row["delivery"] for row in filed] == ["deliver", "duplicate"]
    assert len(inbox.inbox()) == 1


def test_delivery_decision_outside_the_store() -> None:
    rules = _rules()
    now = _at(8, 30)
    recent = [("signal:crypto-signal-engine:s1", _at(8, 0))]
    assert (
        delivery_decision(now, rules, "signal:crypto-signal-engine:s1", recent)
        is Delivery.DUPLICATE
    )
    assert delivery_decision(now, rules, "signal:crypto-signal-engine:other", recent) is Delivery.DELIVER


def test_load_rules_defaults_and_rejects_garbage(monkeypatch) -> None:
    for name in (
        "ALERT_SIGNAL_STRENGTH_MIN",
        "ALERT_STALE_MINUTES",
        "ALERT_DRAWDOWN_PERCENT",
        "ALERT_DEDUPE_MINUTES",
        "ALERT_QUIET_HOURS",
        "BRIEFING_TIME",
    ):
        monkeypatch.delenv(name, raising=False)
    rules = load_rules()
    assert rules.signal_strength_above == Decimal("0.7")
    assert rules.stale_after == timedelta(minutes=15)
    assert rules.drawdown_percent == Decimal("5")
    assert rules.dedupe_window == timedelta(minutes=60)
    assert rules.briefing_at.hour == 8

    monkeypatch.setenv("ALERT_DRAWDOWN_PERCENT", "nope")
    monkeypatch.setenv("ALERT_QUIET_HOURS", "not-a-clock")
    monkeypatch.setenv("BRIEFING_TIME", "09:30")
    monkeypatch.setenv("ALERT_SIGNAL_STRENGTH_MIN", "0.55")
    overridden = load_rules()
    assert overridden.drawdown_percent == Decimal("5")
    assert overridden.quiet_start.hour == 22
    assert overridden.briefing_at.hour == 9
    assert overridden.briefing_at.minute == 30
    assert overridden.signal_strength_above == Decimal("0.55")


def test_briefing_is_due_once_per_ist_day() -> None:
    at = time(8, 0)
    assert briefing.briefing_due(_at(7, 59), at, None) is False
    assert briefing.briefing_due(_at(8, 0), at, None) is True
    assert briefing.briefing_due(_at(18, 0), at, _at(8, 0).date()) is False
    assert briefing.briefing_due(_at(8, 0, day=7), at, _at(8, 0).date()) is True


def test_briefing_sections_carry_as_of_and_do_not_invent(inbox) -> None:
    generated = _at(8, 0)
    inputs = briefing.BriefingInputs(
        market=briefing.MarketInput(available=False, as_of=None, text="BTC 100.00"),
        signals=briefing.SignalsInput(
            available=True,
            as_of="2026-11-06T02:30:00+00:00",
            items=(briefing.SignalLine("BTCUSDT", "buy", "0.72"),),
        ),
        positions=briefing.PositionsInput(
            available=True,
            as_of="2026-11-06T02:29:00+00:00",
            symbols=("RELIANCE",),
        ),
        trades=briefing.TradesInput(
            available=True,
            as_of="2026-11-06T02:28:00+00:00",
            trades=(
                _trade("a", "10", "2026-11-01T00:00:00Z"),
                _trade("b", "-4", "2026-11-02T00:00:00Z"),
            ),
        ),
        alerts=(
            {
                "severity": "warning",
                "title": "BTCUSDT signal 0.72",
                "data_as_of": "2026-11-06T02:30:00+00:00",
            },
        ),
        generated_at=generated,
    )
    sections = {section.name: section for section in briefing.assemble(inputs)}
    assert set(sections) == {"market", "signals", "positions", "performance", "alerts"}
    assert sections["market"].as_of == "unknown"
    assert sections["market"].text == "unavailable"
    assert "100.00" not in sections["market"].render()
    assert sections["signals"].as_of == "2026-11-06T02:30:00+00:00"
    assert "BTCUSDT buy strength 0.72" in sections["signals"].text
    assert sections["positions"].text == "RELIANCE"
    assert "ETHUSDT" not in sections["positions"].render()
    assert "sample size 2" in sections["performance"].text
    assert "2026-11-01T00:00:00+00:00" in sections["performance"].text
    assert sections["performance"].as_of == "2026-11-06T02:28:00+00:00"
    assert sections["alerts"].as_of == generated.isoformat()
    assert "data_as_of 2026-11-06T02:30:00+00:00" in sections["alerts"].text

    mission_id = briefing.persist(inputs)
    mission = mission_store.get_mission(mission_id)
    assert mission is not None
    assert mission.status.value == "COMPLETED"
    for name in ("market", "signals", "positions", "performance", "alerts"):
        assert f"[{name}] as_of" in mission.summary
    tasks = mission_store.list_tasks(mission_id)
    assert len(tasks) == 5
    assert {task.status for task in tasks} == {TaskStatus.SUCCESS}
    assert "ETHUSDT" not in mission.summary
    assert "100.00" not in mission.summary


def test_unavailable_reads_do_not_invent_a_performance_figure() -> None:
    inputs = briefing.BriefingInputs(
        market=briefing.MarketInput(False, None),
        signals=briefing.SignalsInput(False, None),
        positions=briefing.PositionsInput(False, None),
        trades=briefing.TradesInput(False, None, trades=(_trade("a", "10", "2026-11-01T00:00:00Z"),)),
        generated_at=_at(8, 0),
    )
    rendered = "\n".join(section.render() for section in briefing.assemble(inputs))
    assert rendered.count("as_of unknown") == 4
    assert "unavailable" in rendered
    assert "sample size" not in rendered
    assert "BTCUSDT" not in rendered


def test_inbox_route_lists_delivered_only(client) -> None:
    from app.alerts.rules import AlertDraft
    from app.alerts import store

    store.init_db()
    now = _at(8, 0)
    draft = AlertDraft(
        dedupe_key="signal:crypto-signal-engine:s1",
        rule="signal",
        severity="warning",
        title="BTCUSDT signal 0.9",
        body="above 0.7",
        source_engine="crypto-signal-engine",
        data_as_of=now.astimezone(ZoneInfo("UTC")).isoformat(),
    )
    store.file_alerts([draft], _rules(), now)
    quiet = store.file_alerts([draft], _rules(), _at(23, 0))
    assert quiet[0]["delivery"] == "suppressed"

    denied = client.get("/api/v1/alerts", headers={"X-Jarvis-Passcode": "wrong"})
    assert denied.status_code == 401

    listed = client.get("/api/v1/alerts").json()
    assert listed["count"] == 1
    assert listed["alerts"][0]["title"] == "BTCUSDT signal 0.9"
    assert listed["alerts"][0]["status"] == "delivered"


def test_alerts_package_does_not_send_anything() -> None:
    root = Path("app/alerts")
    banned = ("smtplib", "httpx", "requests", "slack", "webhook", "aiosmtplib")
    for path in root.rglob("*.py"):
        text = path.read_text()
        for word in banned:
            assert word not in text, f"{path} mentions {word}"
