"""Daily trading briefing, stored as a finished mission.

The five sections are filled in Python. A model pass can drop an as-of time
or invent a price, and this briefing is the record those times have to survive
in. Tasks are written already SUCCESS so MissionScheduler.start() has nothing
left to plan or re-run. The mission is not dispatched to an engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.alerts.rules import IST
from app.analytics.outcomes import compute_outcomes
from app.missions.models import Mission, MissionStatus, Task, TaskStatus, new_mission_id, new_task_id
from app.missions import store as mission_store

DAILY_GOAL = "Daily trading briefing"
SECTIONS = ("market", "signals", "positions", "performance", "alerts")
_AGENT = "trading_research"


@dataclass(frozen=True)
class MarketInput:
    available: bool
    as_of: str | None
    text: str = ""


@dataclass(frozen=True)
class SignalLine:
    symbol: str
    side: str
    strength: str


@dataclass(frozen=True)
class SignalsInput:
    available: bool
    as_of: str | None
    items: tuple[SignalLine, ...] = ()


@dataclass(frozen=True)
class PositionsInput:
    available: bool
    as_of: str | None
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradesInput:
    available: bool
    as_of: str | None
    trades: tuple[Any, ...] = ()


@dataclass(frozen=True)
class BriefingInputs:
    market: MarketInput
    signals: SignalsInput
    positions: PositionsInput
    trades: TradesInput
    alerts: tuple[dict[str, Any], ...] = ()
    generated_at: datetime = datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))


@dataclass(frozen=True)
class Section:
    name: str
    as_of: str
    text: str

    def render(self) -> str:
        return f"[{self.name}] as_of {self.as_of}\n{self.text}"


def briefing_due(now: datetime, briefing_at: time, last_run_date: date | None) -> bool:
    """Due once the IST clock has reached `briefing_at` and today has not run.

    `last_run_date` is an IST calendar date. A run at 08:00 does not become
    due again at 18:00 the same day.
    """
    local = now.astimezone(IST)
    if local.timetz().replace(tzinfo=None) < briefing_at:
        return False
    return last_run_date != local.date()


def assemble(inputs: BriefingInputs) -> tuple[Section, ...]:
    generated = inputs.generated_at.astimezone(IST).isoformat()
    return (
        _market(inputs.market),
        _signals(inputs.signals),
        _positions(inputs.positions),
        _performance(inputs.trades),
        _alerts(inputs.alerts, generated),
    )


def persist(inputs: BriefingInputs) -> str:
    """Write the briefing as a completed mission. Returns the mission id."""
    sections = assemble(inputs)
    summary = "\n\n".join(section.render() for section in sections)
    stamp = inputs.generated_at.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds")
    mission = Mission(
        mission_id=new_mission_id(),
        goal=DAILY_GOAL,
        status=MissionStatus.COMPLETED,
        summary=summary,
        created_at=stamp,
    )
    mission_store.create_mission(mission)
    # SUCCESS up front: an empty task list would be planned by the echo
    # engine, which can drop an as-of or name a symbol that was not read.
    mission_store.create_tasks(
        [
            Task(
                task_id=new_task_id(),
                mission_id=mission.mission_id,
                agent_id=_AGENT,
                objective=f"Briefing section: {section.name}",
                status=TaskStatus.SUCCESS,
                result=section.render(),
                idempotency_key=f"briefing:{mission.mission_id}:{section.name}",
                created_at=stamp,
            )
            for section in sections
        ]
    )
    return mission.mission_id


def _market(market: MarketInput) -> Section:
    if not market.available:
        return Section("market", "unknown", "unavailable")
    text = market.text.strip() or "no overview text"
    return Section("market", _as_of(market.as_of), text)


def _signals(signals: SignalsInput) -> Section:
    if not signals.available:
        return Section("signals", "unknown", "unavailable")
    if not signals.items:
        return Section("signals", _as_of(signals.as_of), "no signals")
    lines = [
        f"{item.symbol} {item.side} strength {item.strength}"
        for item in signals.items
    ]
    return Section("signals", _as_of(signals.as_of), "\n".join(lines))


def _positions(positions: PositionsInput) -> Section:
    if not positions.available:
        return Section("positions", "unknown", "unavailable")
    symbols = tuple(symbol for symbol in positions.symbols if symbol.strip())
    if not symbols:
        return Section("positions", _as_of(positions.as_of), "no open positions")
    return Section("positions", _as_of(positions.as_of), "\n".join(symbols))


def _performance(trades: TradesInput) -> Section:
    if not trades.available:
        return Section("performance", "unknown", "unavailable")
    analysis = compute_outcomes(trades.trades)
    if not analysis.reports:
        return Section("performance", _as_of(trades.as_of), "sample size 0; period unknown")
    lines = []
    for report in analysis.reports:
        start = report.period_start.isoformat() if report.period_start else "unknown"
        end = report.period_end.isoformat() if report.period_end else "unknown"
        lines.append(
            f"{report.mode} {report.currency}: sample size {report.sample_size}; "
            f"period {start} to {end}"
        )
    return Section("performance", _as_of(trades.as_of), "\n".join(lines))


def _alerts(alerts: tuple[dict[str, Any], ...], generated_as_of: str) -> Section:
    if not alerts:
        return Section("alerts", generated_as_of, "no delivered alerts")
    lines = []
    for alert in alerts:
        as_of = alert.get("data_as_of") or "unknown"
        lines.append(f"{alert.get('severity', '')} {alert.get('title', '')} (data_as_of {as_of})")
    return Section("alerts", generated_as_of, "\n".join(lines))


def _as_of(value: str | None) -> str:
    if value is None or not str(value).strip():
        return "unknown"
    return str(value)
