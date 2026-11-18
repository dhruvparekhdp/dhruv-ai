"""Per-call LLM usage and the spend budgets that pause a mission.

Every successful router call is one row: tokens in, tokens out, provider,
model, and an estimated cost. Budgets are checked against that ledger:

    mission   estimated spend of one mission
    day       estimated spend of the UTC day, across every caller
    agent     estimated spend of one agent during the UTC day

Crossing a budget is a pause and an approval request, decided by the
scheduler. This module only records and reports. It does not fail the call
that already happened: the point of the row is that the spend is visible.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from app.core.db import execute, execute_script, fetch_all, fetch_one, get_conn
from app.core.pricing import estimate

_mission_id: ContextVar[str] = ContextVar("usage_mission_id", default="")
_agent_id: ContextVar[str] = ContextVar("usage_agent_id", default="")

# Defaults are wide enough that the local echo engine (priced at zero) and the
# test suite's small scripted calls do not pause a mission. Tests tighten them.
_DEFAULT_MISSION_USD = Decimal("1")
_DEFAULT_DAY_USD = Decimal("5")
_DEFAULT_AGENT_USD = Decimal("2")


@contextmanager
def scope(mission_id: str, agent_id: str) -> Iterator[None]:
    """Attribute the router calls inside the block to a mission and an agent."""
    mission_token = _mission_id.set(mission_id or "")
    agent_token = _agent_id.set(agent_id or "")
    try:
        yield
    finally:
        _mission_id.reset(mission_token)
        _agent_id.reset(agent_token)


def init_db() -> None:
    with get_conn() as conn:
        execute_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id            TEXT PRIMARY KEY,
                created_at         TEXT NOT NULL,
                mission_id         TEXT NOT NULL DEFAULT '',
                agent_id           TEXT NOT NULL DEFAULT '',
                provider           TEXT NOT NULL,
                model              TEXT NOT NULL,
                prompt_tokens      INTEGER NOT NULL,
                completion_tokens  INTEGER NOT NULL,
                cost_micros        INTEGER NOT NULL,
                price_known        INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_calls_mission ON llm_calls(mission_id);
            CREATE INDEX IF NOT EXISTS idx_llm_calls_agent   ON llm_calls(agent_id);
            """,
        )


def record(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    mission_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Persist one model call. mission_id and agent_id default to the active scope."""
    init_db()
    if mission_id is None:
        mission_id = _mission_id.get()
    if agent_id is None:
        agent_id = _agent_id.get()
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    cost_micros, known = estimate(provider, model, prompt, completion)
    call_id = f"llm_{uuid.uuid4()}"
    created_at = _now()
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO llm_calls (
                call_id, created_at, mission_id, agent_id, provider, model,
                prompt_tokens, completion_tokens, cost_micros, price_known
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id, created_at, mission_id or "", agent_id or "", provider, model,
                prompt, completion, cost_micros, 1 if known else 0,
            ),
        )
    return {
        "call_id": call_id,
        "created_at": created_at,
        "mission_id": mission_id or "",
        "agent_id": agent_id or "",
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost_micros": cost_micros,
        "cost_usd": format_usd(cost_micros),
        "price_known": known,
    }


def blocking(mission_id: str = "", agent_id: str = "") -> str:
    """Why further spend must pause, or "" when every budget still has room.

    A budget is exceeded only when spend is strictly greater than the limit,
    so a priced-at-zero local call never trips a zero-cost total.
    """
    limits = budget_limits()
    reasons: list[str] = []
    if mission_id:
        spent = _sum(mission_id=mission_id)
        if spent > limits["mission_micros"]:
            reasons.append(
                f"mission budget exceeded ({format_usd(spent)} > {format_usd(limits['mission_micros'])} USD)"
            )
    spent_today = _sum(day=_today())
    if spent_today > limits["day_micros"]:
        reasons.append(
            f"daily budget exceeded ({format_usd(spent_today)} > {format_usd(limits['day_micros'])} USD)"
        )
    if agent_id:
        spent_agent = _sum(agent_id=agent_id, day=_today())
        if spent_agent > limits["agent_micros"]:
            reasons.append(
                "agent budget exceeded for "
                f"{agent_id} ({format_usd(spent_agent)} > {format_usd(limits['agent_micros'])} USD)"
            )
    return "; ".join(reasons)


def report(day: str = "", mission_id: str = "") -> dict[str, Any]:
    """Daily, per-agent, and per-mission breakdowns, plus the recent calls."""
    init_db()
    selected = day.strip() or _today()
    limits = budget_limits()
    where, params = _filter(day=selected if day.strip() else "", mission_id=mission_id)

    with get_conn() as conn:
        totals = _aggregate(conn, where, params)
        today_where, today_params = _filter(day=selected, mission_id=mission_id)
        today_totals = _aggregate(conn, today_where, today_params)
        daily = _grouped(conn, "substr(created_at, 1, 10)", "day", where, params)
        by_agent = _grouped(conn, "agent_id", "agent_id", today_where, today_params)
        by_mission = _grouped(conn, "mission_id", "mission_id", today_where, today_params)
        call_sql = (
            "SELECT call_id, created_at, mission_id, agent_id, provider, model,"
            " prompt_tokens, completion_tokens, cost_micros, price_known"
            " FROM llm_calls"
        )
        if where:
            call_sql += " WHERE " + where
        call_sql += " ORDER BY created_at DESC, call_id DESC LIMIT 100"
        calls = fetch_all(conn, call_sql, params)

    return {
        "currency": "USD",
        "today": selected,
        "budgets": {
            "mission_usd": format_usd(limits["mission_micros"]),
            "day_usd": format_usd(limits["day_micros"]),
            "agent_usd": format_usd(limits["agent_micros"]),
        },
        "totals": totals,
        "today_totals": today_totals,
        "daily": daily,
        "by_agent": by_agent,
        "by_mission": by_mission,
        "calls": [_call(row) for row in calls],
    }


def budget_limits() -> dict[str, int]:
    return {
        "mission_micros": _usd_env("JARVIS_BUDGET_MISSION_USD", _DEFAULT_MISSION_USD),
        "day_micros": _usd_env("JARVIS_BUDGET_DAY_USD", _DEFAULT_DAY_USD),
        "agent_micros": _usd_env("JARVIS_BUDGET_AGENT_USD", _DEFAULT_AGENT_USD),
    }


def format_usd(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    amount = abs(int(micros))
    return f"{sign}{amount // 1_000_000}.{amount % 1_000_000:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _usd_env(name: str, default: Decimal) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return int(default * Decimal(1_000_000))
    try:
        return int((Decimal(raw) * Decimal(1_000_000)).to_integral_value())
    except InvalidOperation:
        return int(default * Decimal(1_000_000))


def _sum(*, mission_id: str = "", agent_id: str = "", day: str = "") -> int:
    init_db()
    where, params = _filter(day=day, mission_id=mission_id, agent_id=agent_id)
    sql = "SELECT COALESCE(SUM(cost_micros), 0) AS n FROM llm_calls"
    if where:
        sql += " WHERE " + where
    with get_conn() as conn:
        row = fetch_one(conn, sql, params)
    return int(row["n"] or 0) if row else 0


def _filter(
    *, day: str = "", mission_id: str = "", agent_id: str = ""
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if day:
        start, end = _day_bounds(day)
        clauses.append("created_at >= ? AND created_at < ?")
        params.extend((start, end))
    if mission_id:
        clauses.append("mission_id = ?")
        params.append(mission_id)
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    return " AND ".join(clauses), params


def _day_bounds(day: str) -> tuple[str, str]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return (
        start.isoformat(timespec="microseconds"),
        end.isoformat(timespec="microseconds"),
    )


def _aggregate(conn: Any, where: str, params: list[Any]) -> dict[str, Any]:
    sql = (
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(cost_micros), 0) AS cost_micros FROM llm_calls"
    )
    if where:
        sql += " WHERE " + where
    row = fetch_one(conn, sql, params) or {}
    micros = int(row.get("cost_micros") or 0)
    return {
        "calls": int(row.get("calls") or 0),
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "cost_usd": format_usd(micros),
    }


def _grouped(
    conn: Any, expression: str, key: str, where: str, params: list[Any]
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {expression} AS bucket, COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(cost_micros), 0) AS cost_micros FROM llm_calls"
    )
    if where:
        sql += " WHERE " + where
    sql += f" GROUP BY {expression} ORDER BY bucket ASC"
    rows = []
    for row in fetch_all(conn, sql, params):
        rows.append(
            {
                key: row["bucket"] or "",
                "calls": int(row["calls"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "cost_usd": format_usd(int(row["cost_micros"] or 0)),
            }
        )
    return rows


def _call(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "call_id": row["call_id"],
        "created_at": str(row["created_at"]),
        "mission_id": row["mission_id"] or "",
        "agent_id": row["agent_id"] or "",
        "provider": row["provider"],
        "model": row["model"],
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "cost_usd": format_usd(int(row["cost_micros"] or 0)),
        "price_known": bool(row["price_known"]),
    }
