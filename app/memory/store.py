"""Memory storage: scoped, permissioned, with provenance.

Scopes keep one mission's notes out of another's, and out of long-term memory:

    user      durable facts about the person
    project   durable facts about this build
    mission   scratch notes for one mission, isolated by scope_key

Every entry records **where it came from**. A confidently recalled wrong fact is
worse than no memory, so the UI can always show when something was learned and
which agent or mission wrote it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.db import (
    ddl_types,
    execute,
    execute_script,
    fetch_all,
    fetch_one,
    get_conn,
    is_postgres,
)
from app.memory.embeddings import embedder, pack

VALID_SCOPES = ("user", "project", "mission")

#: Guards against one runaway agent filling the database.
MAX_VALUE_CHARS = 4_000
MAX_ENTRIES_PER_SCOPE = 500


class MemoryError_(ValueError):
    """Invalid memory operation (bad scope, oversized value, full scope)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_scope(scope: str) -> str:
    if scope not in VALID_SCOPES:
        raise MemoryError_(f"scope must be one of {', '.join(VALID_SCOPES)}; got '{scope}'")
    return scope


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

#: Columns added after the table first shipped. Applied on every boot so an
#: existing database gains them without a manual migration step.
_ADDED_COLUMNS = {
    "source": "TEXT DEFAULT ''",
    "embedding": "TEXT",
    "accessed_at": "{ts}",
    "access_count": "INTEGER DEFAULT 0",
    "superseded_by": "TEXT",
}


def _existing_columns(conn: Any, table: str) -> set[str]:
    if is_postgres():
        rows = fetch_all(
            conn,
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        )
        return {r["column_name"] for r in rows}
    rows = fetch_all(conn, f"PRAGMA table_info({table})", ())
    return {r["name"] for r in rows}


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS memory_entries (
        entry_id      TEXT PRIMARY KEY,
        scope         TEXT NOT NULL,
        scope_key     TEXT NOT NULL DEFAULT '',
        mem_key       TEXT NOT NULL,
        value         TEXT NOT NULL,
        created_at    {ts},
        updated_at    {ts}
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_unique
        ON memory_entries(scope, scope_key, mem_key);
    """.format(**t)

    with get_conn() as conn:
        execute_script(conn, script)
        have = _existing_columns(conn, "memory_entries")
        for column, spec in _ADDED_COLUMNS.items():
            if column not in have:
                execute(
                    conn,
                    f"ALTER TABLE memory_entries ADD COLUMN {column} {spec.format(**t)}",
                )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def write(
    scope: str,
    key: str,
    value: str,
    *,
    scope_key: str = "",
    source: str = "user",
) -> dict[str, Any]:
    """Create or update one entry. Returns the stored row."""
    validate_scope(scope)
    if not key.strip():
        raise MemoryError_("key must not be empty")
    if not value.strip():
        raise MemoryError_("value must not be empty")
    if len(value) > MAX_VALUE_CHARS:
        raise MemoryError_(f"value exceeds {MAX_VALUE_CHARS} characters; summarise it first")

    stamp = _now()
    # Embed key and value together: searches phrase things either way.
    vector = embedder.embed_one(f"{key}: {value}")
    blob = pack(vector) if vector else None

    with get_conn() as conn:
        existing = fetch_one(
            conn,
            "SELECT entry_id FROM memory_entries WHERE scope = ? AND scope_key = ? AND mem_key = ?",
            (scope, scope_key, key),
        )
        if existing:
            entry_id = existing["entry_id"]
            execute(
                conn,
                "UPDATE memory_entries SET value = ?, updated_at = ?, source = ?, embedding = ?"
                " WHERE entry_id = ?",
                (value, stamp, source, blob, entry_id),
            )
        else:
            count = fetch_one(
                conn,
                "SELECT COUNT(*) AS n FROM memory_entries WHERE scope = ? AND scope_key = ?",
                (scope, scope_key),
            )
            if int(count["n"]) >= MAX_ENTRIES_PER_SCOPE:
                raise MemoryError_(f"scope '{scope}' is full ({MAX_ENTRIES_PER_SCOPE} entries)")

            entry_id = f"mem_{uuid.uuid4()}"
            execute(
                conn,
                """
                INSERT INTO memory_entries (entry_id, scope, scope_key, mem_key, value,
                                            created_at, updated_at, source, embedding, access_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (entry_id, scope, scope_key, key, value, stamp, stamp, source, blob),
            )

    return get(entry_id) or {}


def forget(scope: str, key: str, *, scope_key: str = "") -> bool:
    validate_scope(scope)
    with get_conn() as conn:
        existing = fetch_one(
            conn,
            "SELECT entry_id FROM memory_entries WHERE scope = ? AND scope_key = ? AND mem_key = ?",
            (scope, scope_key, key),
        )
        if not existing:
            return False
        execute(conn, "DELETE FROM memory_entries WHERE entry_id = ?", (existing["entry_id"],))
    return True


def delete_by_id(entry_id: str) -> bool:
    with get_conn() as conn:
        existing = fetch_one(
            conn, "SELECT entry_id FROM memory_entries WHERE entry_id = ?", (entry_id,)
        )
        if not existing:
            return False
        execute(conn, "DELETE FROM memory_entries WHERE entry_id = ?", (entry_id,))
    return True


def update_by_id(entry_id: str, value: str) -> dict[str, Any] | None:
    """Edit an entry's value from the UI, re-embedding it."""
    if len(value) > MAX_VALUE_CHARS:
        raise MemoryError_(f"value exceeds {MAX_VALUE_CHARS} characters")

    current = get(entry_id)
    if current is None:
        return None

    vector = embedder.embed_one(f"{current['mem_key']}: {value}")
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE memory_entries SET value = ?, updated_at = ?, source = ?, embedding = ?"
            " WHERE entry_id = ?",
            (value, _now(), "user", pack(vector) if vector else None, entry_id),
        )
    return get(entry_id)


def touch(entry_ids: list[str]) -> None:
    """Record that entries were retrieved -- relevance signal for later phases."""
    if not entry_ids:
        return
    stamp = _now()
    with get_conn() as conn:
        for entry_id in entry_ids:
            execute(
                conn,
                "UPDATE memory_entries SET accessed_at = ?,"
                " access_count = COALESCE(access_count, 0) + 1 WHERE entry_id = ?",
                (stamp, entry_id),
            )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_FIELDS = (
    "entry_id, scope, scope_key, mem_key, value, source, created_at, updated_at,"
    " accessed_at, access_count"
)


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    for field in ("created_at", "updated_at", "accessed_at"):
        if row.get(field) is not None:
            row[field] = str(row[field])
    row["access_count"] = row.get("access_count") or 0
    row["source"] = row.get("source") or ""
    return row


def get(entry_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = fetch_one(conn, f"SELECT {_FIELDS} FROM memory_entries WHERE entry_id = ?", (entry_id,))
    return _clean(row) if row else None


def read(scope: str, key: str = "", *, scope_key: str = "") -> list[dict[str, Any]]:
    validate_scope(scope)
    with get_conn() as conn:
        if key:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries"
                " WHERE scope = ? AND scope_key = ? AND mem_key = ?",
                (scope, scope_key, key),
            )
        else:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries WHERE scope = ? AND scope_key = ?"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT 100",
                (scope, scope_key),
            )
    return [_clean(r) for r in rows]


def list_all(scope: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Everything, for the Memory panel. Optionally filtered to one scope."""
    with get_conn() as conn:
        if scope:
            validate_scope(scope)
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries WHERE scope = ?"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT ?",
                (scope, limit),
            )
        else:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT ?",
                (limit,),
            )
    return [_clean(r) for r in rows]


def candidates(scope: str = "", scope_key: str = "") -> list[dict[str, Any]]:
    """Rows plus embeddings, for search to score in memory.

    Fetching and ranking in Python keeps one implementation across both
    backends. At personal scale this is trivially fast -- a few thousand
    384-float vectors score in milliseconds. Revisit only if the corpus grows
    past ~100k entries.
    """
    sql = f"SELECT {_FIELDS}, embedding FROM memory_entries"
    params: list[Any] = []
    clauses: list[str] = []
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
        if scope == "mission":
            clauses.append("scope_key = ?")
            params.append(scope_key)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " LIMIT 5000"

    with get_conn() as conn:
        rows = fetch_all(conn, sql, params)
    return rows


def stats() -> dict[str, Any]:
    with get_conn() as conn:
        total = fetch_one(conn, "SELECT COUNT(*) AS n FROM memory_entries", ())["n"]
        embedded = fetch_one(
            conn, "SELECT COUNT(*) AS n FROM memory_entries WHERE embedding IS NOT NULL", ()
        )["n"]
        by_scope = fetch_all(
            conn, "SELECT scope, COUNT(*) AS n FROM memory_entries GROUP BY scope", ()
        )
    return {
        "total": int(total),
        "embedded": int(embedded),
        "by_scope": {r["scope"]: int(r["n"]) for r in by_scope},
    }
