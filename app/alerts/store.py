"""Durable in-app inbox.

A delivered row is the only thing the inbox lists, and the only thing that
holds a dedupe window. A quiet-hours row is stored as suppressed so the
decision is auditable, and it is not listed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.alerts.rules import SEVERITIES, AlertDraft, AlertRules, Delivery, delivery_decision

DELIVERED = "delivered"
SUPPRESSED = "suppressed"
from app.core.db import ddl_types, execute, execute_script, fetch_all, get_conn


def _parse_ts(value: Any) -> datetime | None:
    """Same two-backend normalisation as the todo store.

    Postgres returns a datetime. SQLite returns the ISO string we wrote.
    Dedupe compares instants, so a string sort would be the wrong clock.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS alerts (
        alert_id      TEXT PRIMARY KEY,
        dedupe_key    TEXT NOT NULL,
        rule          TEXT NOT NULL,
        severity      TEXT NOT NULL,
        title         TEXT NOT NULL,
        body          TEXT NOT NULL,
        source_engine TEXT NOT NULL,
        data_as_of    TEXT,
        status        TEXT NOT NULL,
        created_at    {ts}
    );

    CREATE INDEX IF NOT EXISTS idx_alerts_status_created
        ON alerts (status, created_at);
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)


def file_alerts(
    drafts: list[AlertDraft],
    rules: AlertRules,
    now: datetime,
) -> list[dict[str, Any]]:
    """Apply quiet hours and dedupe, then persist what is not a duplicate.

    A second draft with the same key in this batch sees the row just
    delivered, so one evaluation cannot inbox the same condition twice.
    """
    recent = delivered_keys()
    written: list[dict[str, Any]] = []
    for draft in drafts:
        decision = delivery_decision(now, rules, draft.dedupe_key, recent)
        if decision is Delivery.DUPLICATE:
            written.append({"delivery": decision.value, "dedupe_key": draft.dedupe_key})
            continue
        status = DELIVERED if decision is Delivery.DELIVER else SUPPRESSED
        row = insert(draft, status=status, created_at=now)
        row["delivery"] = decision.value
        if decision is Delivery.DELIVER:
            recent.append((draft.dedupe_key, now))
        written.append(row)
    return written


def insert(draft: AlertDraft, *, status: str, created_at: datetime) -> dict[str, Any]:
    if draft.severity not in SEVERITIES:
        raise ValueError(f"unknown severity {draft.severity}")
    if status not in {DELIVERED, SUPPRESSED}:
        raise ValueError(f"unknown alert status {status}")
    alert_id = f"alrt_{uuid.uuid4()}"
    stamp = created_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO alerts (
                alert_id, dedupe_key, rule, severity, title, body,
                source_engine, data_as_of, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id, draft.dedupe_key, draft.rule, draft.severity, draft.title,
                draft.body, draft.source_engine, draft.data_as_of, status, stamp,
            ),
        )
    return {
        "alert_id": alert_id,
        "dedupe_key": draft.dedupe_key,
        "rule": draft.rule,
        "severity": draft.severity,
        "title": draft.title,
        "body": draft.body,
        "source_engine": draft.source_engine,
        "data_as_of": draft.data_as_of,
        "status": status,
        "created_at": stamp,
    }


def inbox() -> list[dict[str, Any]]:
    """Delivered alerts only, newest first."""
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT alert_id, dedupe_key, rule, severity, title, body,
                   source_engine, data_as_of, status, created_at
            FROM alerts
            WHERE status = ?
            ORDER BY created_at DESC, alert_id DESC
            """,
            (DELIVERED,),
        )
    return [_public(row) for row in rows]


def delivered_keys() -> list[tuple[str, datetime]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT dedupe_key, created_at FROM alerts WHERE status = ?",
            (DELIVERED,),
        )
    found: list[tuple[str, datetime]] = []
    for row in rows:
        parsed = _parse_ts(row["created_at"])
        if parsed is None:
            continue
        found.append((str(row["dedupe_key"]), parsed))
    return found


def _public(row: dict[str, Any]) -> dict[str, Any]:
    created = _parse_ts(row["created_at"])
    return {
        "alert_id": row["alert_id"],
        "dedupe_key": row["dedupe_key"],
        "rule": row["rule"],
        "severity": row["severity"],
        "title": row["title"],
        "body": row["body"],
        "source_engine": row["source_engine"],
        "data_as_of": row["data_as_of"],
        "status": row["status"],
        "created_at": created.isoformat() if created else str(row["created_at"]),
    }
