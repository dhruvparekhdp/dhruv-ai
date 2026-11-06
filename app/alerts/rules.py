"""Alert rules, IST quiet hours, and the dedupe decision.

The comparisons are strict. "Above strength X" and "drawdown > Y%" do not
fire on an equal value. "Stale > N minutes" does not fire when the age is
exactly N. A quiet-hours row is not a delivery, so it does not start the
dedupe window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics.outcomes import compute_outcomes

# Wall clock for quiet hours and the briefing. The machine zone is not IST,
# and a UTC comparison would silence the wrong part of Dhruv's day.
IST = ZoneInfo("Asia/Kolkata")

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL})

RULE_SIGNAL = "signal"
RULE_STALE = "stale"
RULE_DRAWDOWN = "drawdown"
RULE_RISK = "risk"


class Delivery(str, Enum):
    DELIVER = "deliver"
    SUPPRESSED = "suppressed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class AlertRules:
    """Thresholds. `drawdown_percent` is Y in "drawdown > Y%" (5 means 5%)."""

    signal_strength_above: Decimal = Decimal("0.7")
    stale_after: timedelta = timedelta(minutes=15)
    drawdown_percent: Decimal = Decimal("5")
    dedupe_window: timedelta = timedelta(minutes=60)
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    briefing_at: time = time(8, 0)


@dataclass(frozen=True)
class SignalFact:
    signal_id: str
    symbol: str
    strength: Decimal
    status: str


@dataclass(frozen=True)
class EngineSnapshot:
    source_engine: str
    data_as_of: datetime | None
    signals: tuple[SignalFact, ...] = ()
    trades: tuple[Any, ...] = ()
    breaches: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertDraft:
    dedupe_key: str
    rule: str
    severity: str
    title: str
    body: str
    source_engine: str
    data_as_of: str | None


def load_rules() -> AlertRules:
    """Read thresholds from the environment. A bad value keeps the default.

    These names are not secrets. A token must never be placed in one of them.
    """
    quiet_start, quiet_end = _quiet_window(os.getenv("ALERT_QUIET_HOURS"))
    return AlertRules(
        signal_strength_above=_decimal(
            os.getenv("ALERT_SIGNAL_STRENGTH_MIN"), Decimal("0.7")
        ),
        stale_after=timedelta(minutes=_minutes(os.getenv("ALERT_STALE_MINUTES"), 15)),
        drawdown_percent=_decimal(os.getenv("ALERT_DRAWDOWN_PERCENT"), Decimal("5")),
        dedupe_window=timedelta(minutes=_minutes(os.getenv("ALERT_DEDUPE_MINUTES"), 60)),
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        briefing_at=_clock(os.getenv("BRIEFING_TIME"), time(8, 0)),
    )


def in_quiet_hours(now: datetime, start: time, end: time) -> bool:
    """True inside [start, end) on the IST wall clock.

    When start is later than end the window wraps midnight, so 22:00–07:00
    covers the evening and the early morning and leaves 07:00 itself outside.
    A zero-length window (start == end) is never quiet.
    """
    local = _require_aware(now).astimezone(IST).timetz().replace(tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def delivery_decision(
    now: datetime,
    rules: AlertRules,
    dedupe_key: str,
    recent_delivered: list[tuple[str, datetime]],
) -> Delivery:
    """Quiet hours win over dedupe. Only a delivered row holds the window."""
    if in_quiet_hours(now, rules.quiet_start, rules.quiet_end):
        return Delivery.SUPPRESSED
    current = _require_aware(now).astimezone(timezone.utc)
    cutoff = current - rules.dedupe_window
    for key, delivered_at in recent_delivered:
        if key != dedupe_key:
            continue
        at = _require_aware(delivered_at).astimezone(timezone.utc)
        if at >= cutoff:
            return Delivery.DUPLICATE
    return Delivery.DELIVER


def evaluate(snapshot: EngineSnapshot, rules: AlertRules, now: datetime) -> list[AlertDraft]:
    """Drafts for one engine. Delivery is a separate step."""
    moment = _require_aware(now)
    drafts: list[AlertDraft] = []
    drafts.extend(_signals(snapshot, rules))
    stale = _stale(snapshot, rules, moment)
    if stale is not None:
        drafts.append(stale)
    drafts.extend(_drawdowns(snapshot, rules))
    drafts.extend(_breaches(snapshot))
    return drafts


def _signals(snapshot: EngineSnapshot, rules: AlertRules) -> list[AlertDraft]:
    drafts: list[AlertDraft] = []
    for signal in snapshot.signals:
        if signal.status != "active":
            continue
        strength = Decimal(signal.strength)
        if strength <= rules.signal_strength_above:
            continue
        drafts.append(
            AlertDraft(
                dedupe_key=f"signal:{snapshot.source_engine}:{signal.signal_id}",
                rule=RULE_SIGNAL,
                severity=SEVERITY_WARNING,
                title=f"{signal.symbol} signal {strength}",
                body=(
                    f"{snapshot.source_engine} active signal {signal.signal_id} "
                    f"on {signal.symbol} strength {strength}, "
                    f"above {rules.signal_strength_above}."
                ),
                source_engine=snapshot.source_engine,
                data_as_of=_stamp(snapshot.data_as_of),
            )
        )
    return drafts


def _stale(snapshot: EngineSnapshot, rules: AlertRules, now: datetime) -> AlertDraft | None:
    # No timestamp means we do not know the age. That is not "older than N".
    if snapshot.data_as_of is None:
        return None
    age = now - _require_aware(snapshot.data_as_of)
    if age <= rules.stale_after:
        return None
    minutes = int(age.total_seconds() // 60)
    return AlertDraft(
        dedupe_key=f"stale:{snapshot.source_engine}",
        rule=RULE_STALE,
        severity=SEVERITY_WARNING,
        title=f"{snapshot.source_engine} data is {minutes} min old",
        body=(
            f"{snapshot.source_engine} data_as_of {_stamp(snapshot.data_as_of)} "
            f"is older than {int(rules.stale_after.total_seconds() // 60)} min."
        ),
        source_engine=snapshot.source_engine,
        data_as_of=_stamp(snapshot.data_as_of),
    )


def _drawdowns(snapshot: EngineSnapshot, rules: AlertRules) -> list[AlertDraft]:
    if not snapshot.trades:
        return []
    analysis = compute_outcomes(snapshot.trades)
    limit = rules.drawdown_percent / Decimal(100)
    drafts: list[AlertDraft] = []
    for report in analysis.reports:
        pct = report.max_drawdown_pct
        if pct is None or pct <= limit:
            continue
        drafts.append(
            AlertDraft(
                dedupe_key=f"drawdown:{report.mode}:{report.currency}",
                rule=RULE_DRAWDOWN,
                severity=SEVERITY_CRITICAL,
                title=(
                    f"{report.mode} {report.currency} drawdown "
                    f"{_percent(pct)}%"
                ),
                body=(
                    f"Max drawdown {_percent(pct)}% of peak on the {report.mode} "
                    f"{report.currency} book (sample {report.sample_size}), "
                    f"above {rules.drawdown_percent}%."
                ),
                source_engine=snapshot.source_engine,
                data_as_of=_stamp(snapshot.data_as_of),
            )
        )
    return drafts


def _breaches(snapshot: EngineSnapshot) -> list[AlertDraft]:
    drafts: list[AlertDraft] = []
    seen: set[str] = set()
    for name in snapshot.breaches:
        cleaned = name.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        drafts.append(
            AlertDraft(
                dedupe_key=f"risk:{snapshot.source_engine}:{cleaned}",
                rule=RULE_RISK,
                severity=SEVERITY_CRITICAL,
                title=f"Risk limit {cleaned}",
                body=(
                    f"{snapshot.source_engine} reported a breach of {cleaned}."
                ),
                source_engine=snapshot.source_engine,
                data_as_of=_stamp(snapshot.data_as_of),
            )
        )
    return drafts


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_aware(value).astimezone(timezone.utc).isoformat()


def _percent(ratio: Decimal) -> str:
    return format(ratio * Decimal(100), "f")


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _decimal(raw: str | None, default: Decimal) -> Decimal:
    if raw is None or not raw.strip():
        return default
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return default


def _minutes(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def _clock(raw: str | None, default: time) -> time:
    if raw is None or not raw.strip():
        return default
    parsed = _parse_clock(raw.strip())
    return parsed if parsed is not None else default


def _quiet_window(raw: str | None) -> tuple[time, time]:
    default = (time(22, 0), time(7, 0))
    if raw is None or not raw.strip():
        return default
    parts = raw.strip().split("-")
    if len(parts) != 2:
        return default
    start = _parse_clock(parts[0].strip())
    end = _parse_clock(parts[1].strip())
    if start is None or end is None:
        return default
    return start, end


def _parse_clock(raw: str) -> time | None:
    pieces = raw.split(":")
    if len(pieces) != 2:
        return None
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
    except ValueError:
        return None
    if hour > 23 or minute > 59 or hour < 0 or minute < 0:
        return None
    return time(hour, minute)
