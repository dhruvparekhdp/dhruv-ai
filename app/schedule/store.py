"""Persisted schedule jobs — survives restarts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn

TICK_SECONDS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS schedule_jobs (
        name            TEXT PRIMARY KEY,
        interval_seconds INTEGER NOT NULL,
        last_run_at     {ts},
        next_run_at     {ts} NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1
    );
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)
        defaults = (
            ("reminders", 300),
            ("watchers", 60),
        )
        for name, interval in defaults:
            if fetch_one(conn, "SELECT name FROM schedule_jobs WHERE name = ?", (name,)):
                continue
            due = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="microseconds")
            execute(
                conn,
                "INSERT INTO schedule_jobs (name, interval_seconds, last_run_at, next_run_at, enabled)"
                " VALUES (?, ?, NULL, ?, 1)",
                (name, interval, due),
            )


def list_jobs() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT * FROM schedule_jobs ORDER BY name")
    return [dict(row) for row in rows]


def due_jobs(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    now_s = now.isoformat(timespec="microseconds")
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM schedule_jobs WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
            (now_s,),
        )
    return [dict(row) for row in rows]


def mark_started(name: str, *, now: datetime | None = None) -> None:
    """Record a run *before* side effects so a crash re-runs at most once."""
    now = now or datetime.now(timezone.utc)
    now_s = now.isoformat(timespec="microseconds")
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT interval_seconds FROM schedule_jobs WHERE name = ?", (name,))
        if not row:
            return
        interval = int(row["interval_seconds"])
        next_at = (now + timedelta(seconds=interval)).isoformat(timespec="microseconds")
        execute(
            conn,
            "UPDATE schedule_jobs SET last_run_at = ?, next_run_at = ? WHERE name = ?",
            (now_s, next_at, name),
        )
