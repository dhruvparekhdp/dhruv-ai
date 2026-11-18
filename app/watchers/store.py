"""Watcher config and check history."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn

State = str  # "up" | "down" | "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS watchers (
        name            TEXT PRIMARY KEY,
        url             TEXT NOT NULL,
        expect_status   INTEGER NOT NULL DEFAULT 200,
        expect_json_path TEXT DEFAULT '',
        interval_seconds INTEGER NOT NULL DEFAULT 300,
        state           TEXT NOT NULL DEFAULT 'unknown',
        enabled         INTEGER NOT NULL DEFAULT 1,
        updated_at      {ts} NOT NULL
    );
    CREATE TABLE IF NOT EXISTS watcher_checks (
        check_id        TEXT PRIMARY KEY,
        watcher_name    TEXT NOT NULL,
        checked_at      {ts} NOT NULL,
        ok              INTEGER NOT NULL,
        status_code     INTEGER,
        detail          TEXT DEFAULT ''
    );
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)
    _seed_from_env()


def _seed_from_env() -> None:
    raw = os.getenv("JARVIS_WATCHERS_JSON", "").strip()
    if not raw:
        return
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
            continue
        upsert(
            name=str(item["name"]),
            url=str(item["url"]),
            expect_status=int(item.get("expect_status", 200)),
            expect_json_path=str(item.get("expect_json_path", "")),
            interval_seconds=int(item.get("interval_seconds", 300)),
        )


def upsert(
    *,
    name: str,
    url: str,
    expect_status: int = 200,
    expect_json_path: str = "",
    interval_seconds: int = 300,
    enabled: bool = True,
) -> None:
    with get_conn() as conn:
        existing = fetch_one(conn, "SELECT name FROM watchers WHERE name = ?", (name,))
        if existing:
            execute(
                conn,
                """
                UPDATE watchers
                   SET url = ?, expect_status = ?, expect_json_path = ?,
                       interval_seconds = ?, enabled = ?, updated_at = ?
                 WHERE name = ?
                """,
                (url, expect_status, expect_json_path, interval_seconds, int(enabled), _now(), name),
            )
        else:
            execute(
                conn,
                """
                INSERT INTO watchers (name, url, expect_status, expect_json_path,
                                      interval_seconds, state, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)
                """,
                (name, url, expect_status, expect_json_path, interval_seconds, int(enabled), _now()),
            )


def list_watchers() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT * FROM watchers ORDER BY name")
    return [dict(row) for row in rows]


def get(name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM watchers WHERE name = ?", (name,))
    return dict(row) if row else None


def set_state(name: str, state: State) -> None:
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE watchers SET state = ?, updated_at = ? WHERE name = ?",
            (state, _now(), name),
        )


def record_check(
    *,
    watcher_name: str,
    ok: bool,
    status_code: int | None,
    detail: str = "",
) -> None:
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO watcher_checks (check_id, watcher_name, checked_at, ok, status_code, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"chk_{uuid.uuid4()}", watcher_name, _now(), int(ok), status_code, detail[:2000]),
        )
