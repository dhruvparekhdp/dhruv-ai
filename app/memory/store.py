"""Memory storage: scoped, permissioned, with provenance.

Scopes keep one mission's notes out of another's, and out of long-term memory:

    user      durable facts about the person
    project   durable facts about this build
    mission   scratch notes for one mission, isolated by scope_key

Every entry records where it came from. A correction does not overwrite that
record: it inserts a new entry and points the old one at it. Retrieval returns
only the current version, so a wrong fact stays inspectable without being
what the assistant recalls.

Secrets are refused. A redacted placeholder would still be a stored fact.
"""

from __future__ import annotations

import re
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
from app.memory.secrets import secret_label

VALID_SCOPES = ("user", "project", "mission")
VALID_SOURCES = ("user", "agent", "tool", "import")
VALID_SENSITIVITY = ("normal", "private")

#: Guards against one runaway agent filling the database.
MAX_VALUE_CHARS = 4_000
MAX_ENTRIES_PER_SCOPE = 500

_SOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

#: Current rows only. History shares the same key, so the unique index cannot
#: cover superseded rows. Expired rows stay out of retrieval until the purge
#: job deletes them.
_CURRENT = "(superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?))"


class MemoryError_(ValueError):
    """Invalid memory operation (bad scope, oversized value, full scope)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_scope(scope: str) -> str:
    if scope not in VALID_SCOPES:
        raise MemoryError_(f"scope must be one of {', '.join(VALID_SCOPES)}; got '{scope}'")
    return scope


def validate_source(source: str) -> str:
    if source not in VALID_SOURCES:
        raise MemoryError_(f"source must be one of {', '.join(VALID_SOURCES)}; got '{source}'")
    return source


def validate_sensitivity(sensitivity: str) -> str:
    if sensitivity not in VALID_SENSITIVITY:
        raise MemoryError_(
            f"sensitivity must be one of {', '.join(VALID_SENSITIVITY)}; got '{sensitivity}'"
        )
    return sensitivity


def validate_confidence(confidence: float) -> float:
    try:
        number = float(confidence)
    except (TypeError, ValueError) as exc:
        raise MemoryError_("confidence must be a number from 0 to 1") from exc
    if number != number or number < 0 or number > 1:  # NaN, or outside the range
        raise MemoryError_("confidence must be a number from 0 to 1")
    return number


def validate_source_ref(source_ref: str) -> str:
    """A mission id or a message id. Empty when the writer has neither."""
    text = (source_ref or "").strip()
    if text and _SOURCE_REF.fullmatch(text) is None:
        raise MemoryError_("source_ref must be a mission or message id")
    return text


def validate_expires_at(expires_at: str | None) -> str | None:
    if expires_at is None or str(expires_at).strip() == "":
        return None
    text = str(expires_at).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MemoryError_("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refuse_secrets(key: str, value: str) -> None:
    label = secret_label(key, value)
    if label:
        # The label is the kind of credential, not the credential itself.
        raise MemoryError_(f"refusing to store a {label}; secrets are not kept in memory")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

#: Columns added after the table first shipped. Applied on every boot so an
#: existing database gains them without a manual migration step.
_ADDED_COLUMNS = {
    "source": "TEXT DEFAULT ''",
    "source_ref": "TEXT DEFAULT ''",
    "confidence": "REAL DEFAULT 1",
    "sensitivity": "TEXT DEFAULT 'normal'",
    "expires_at": "{ts}",
    "supersedes": "TEXT",
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

    CREATE TABLE IF NOT EXISTS memory_audit (
        audit_id    TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL,
        scope       TEXT NOT NULL,
        scope_key   TEXT NOT NULL DEFAULT '',
        mem_key     TEXT NOT NULL,
        action      TEXT NOT NULL,
        created_at  {ts}
    );
    CREATE INDEX IF NOT EXISTS idx_memory_audit_entry ON memory_audit(entry_id);
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
        # The original index forbade two rows with one key, which made a
        # correction unable to keep the previous version.
        execute(conn, "DROP INDEX IF EXISTS idx_memory_unique")
        execute(
            conn,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_current"
            " ON memory_entries(scope, scope_key, mem_key)"
            " WHERE superseded_by IS NULL",
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
    source_ref: str = "",
    confidence: float = 1.0,
    sensitivity: str = "normal",
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Insert a fact. If the key already has a current row, supersede it.

    The previous row is kept and linked. Callers that read memory see only
    the new row.
    """
    validate_scope(scope)
    source = validate_source(source)
    source_ref = validate_source_ref(source_ref)
    sensitivity = validate_sensitivity(sensitivity)
    confidence = validate_confidence(confidence)
    expires_at = validate_expires_at(expires_at)
    if not key.strip():
        raise MemoryError_("key must not be empty")
    if not value.strip():
        raise MemoryError_("value must not be empty")
    if len(value) > MAX_VALUE_CHARS:
        raise MemoryError_(f"value exceeds {MAX_VALUE_CHARS} characters; summarise it first")
    _refuse_secrets(key, value)

    stamp = _now()
    # Embed key and value together: searches phrase things either way.
    vector = embedder.embed_one(f"{key}: {value}")
    blob = pack(vector) if vector else None
    entry_id = f"mem_{uuid.uuid4()}"

    with get_conn() as conn:
        existing = fetch_one(
            conn,
            "SELECT entry_id FROM memory_entries"
            " WHERE scope = ? AND scope_key = ? AND mem_key = ? AND superseded_by IS NULL",
            (scope, scope_key, key),
        )
        supersedes = existing["entry_id"] if existing else None
        if existing is None:
            count = fetch_one(
                conn,
                "SELECT COUNT(*) AS n FROM memory_entries"
                " WHERE scope = ? AND scope_key = ? AND superseded_by IS NULL",
                (scope, scope_key),
            )
            if int(count["n"]) >= MAX_ENTRIES_PER_SCOPE:
                raise MemoryError_(f"scope '{scope}' is full ({MAX_ENTRIES_PER_SCOPE} entries)")
        else:
            # Drop the previous row out of the unique index before the new
            # row claims the same key. Both statements share this transaction.
            execute(
                conn,
                "UPDATE memory_entries SET superseded_by = ?, updated_at = ? WHERE entry_id = ?",
                (entry_id, stamp, supersedes),
            )

        execute(
            conn,
            """
            INSERT INTO memory_entries (
                entry_id, scope, scope_key, mem_key, value, created_at, updated_at,
                source, source_ref, confidence, sensitivity, expires_at, supersedes,
                embedding, access_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                entry_id, scope, scope_key, key, value, stamp, stamp,
                source, source_ref, confidence, sensitivity, expires_at, supersedes,
                blob,
            ),
        )

    return get(entry_id) or {}


def forget(scope: str, key: str, *, scope_key: str = "") -> bool:
    """Hard-delete the current fact and every version it replaced."""
    validate_scope(scope)
    with get_conn() as conn:
        existing = fetch_one(
            conn,
            "SELECT entry_id FROM memory_entries"
            " WHERE scope = ? AND scope_key = ? AND mem_key = ? AND superseded_by IS NULL",
            (scope, scope_key, key),
        )
        if not existing:
            return False
        _hard_delete(conn, existing["entry_id"], "delete")
    return True


def delete_by_id(entry_id: str) -> bool:
    """Hard-delete one fact, including the versions it replaced or that replaced it."""
    with get_conn() as conn:
        existing = fetch_one(
            conn, "SELECT entry_id FROM memory_entries WHERE entry_id = ?", (entry_id,)
        )
        if not existing:
            return False
        _hard_delete(conn, entry_id, "delete")
    return True


def update_by_id(entry_id: str, value: str) -> dict[str, Any] | None:
    """Correct an entry from the UI.

    The correction is a new row. The row the user edited stays, linked, so
    the previous belief is still visible by id and is no longer retrieved.
    """
    current = get(entry_id)
    if current is None:
        return None
    while current.get("superseded_by"):
        nxt = get(current["superseded_by"])
        if nxt is None:
            break
        current = nxt
    return write(
        current["scope"],
        current["mem_key"],
        value,
        scope_key=current.get("scope_key") or "",
        source="user",
        sensitivity=current.get("sensitivity") or "normal",
        confidence=float(current.get("confidence") if current.get("confidence") is not None else 1),
        expires_at=current.get("expires_at"),
    )


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


def purge_expired(now: str | None = None) -> int:
    """Hard-delete entries whose expires_at has passed, and audit each one.

    When the current version of a fact expires, the versions it replaced are
    deleted with it. Otherwise an expired fact would survive in history.
    """
    stamp = validate_expires_at(now) if now else _now()
    if stamp is None:
        stamp = _now()
    removed = 0
    with get_conn() as conn:
        due = fetch_all(
            conn,
            "SELECT entry_id, superseded_by FROM memory_entries"
            " WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (stamp,),
        )
        seen: set[str] = set()
        for row in due:
            entry_id = row["entry_id"]
            if entry_id in seen:
                continue
            if row.get("superseded_by"):
                if _delete_one(conn, entry_id, "purge"):
                    seen.add(entry_id)
                    removed += 1
                continue
            for item in _chain(conn, entry_id):
                if item["entry_id"] in seen:
                    continue
                if _delete_one(conn, item["entry_id"], "purge"):
                    seen.add(item["entry_id"])
                    removed += 1
    return removed


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_FIELDS = (
    "entry_id, scope, scope_key, mem_key, value, source, source_ref, confidence,"
    " sensitivity, created_at, updated_at, expires_at, supersedes, superseded_by,"
    " accessed_at, access_count"
)


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    for field in ("created_at", "updated_at", "accessed_at", "expires_at"):
        if row.get(field) is not None:
            row[field] = str(row[field])
    row["access_count"] = row.get("access_count") or 0
    row["source"] = row.get("source") or ""
    row["source_ref"] = row.get("source_ref") or ""
    row["sensitivity"] = row.get("sensitivity") or "normal"
    confidence = row.get("confidence")
    row["confidence"] = 1.0 if confidence is None else float(confidence)
    row["supersedes"] = row.get("supersedes") or None
    row["superseded_by"] = row.get("superseded_by") or None
    return row


def get(entry_id: str) -> dict[str, Any] | None:
    """One row by id, including a version that is no longer current.

    Retrieval (`read`, `list_all`, search) hides those. A direct lookup is how
    a correction's predecessor stays inspectable.
    """
    with get_conn() as conn:
        row = fetch_one(conn, f"SELECT {_FIELDS} FROM memory_entries WHERE entry_id = ?", (entry_id,))
    return _clean(row) if row else None


def read(scope: str, key: str = "", *, scope_key: str = "") -> list[dict[str, Any]]:
    validate_scope(scope)
    stamp = _now()
    with get_conn() as conn:
        if key:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries"
                f" WHERE scope = ? AND scope_key = ? AND mem_key = ? AND {_CURRENT}",
                (scope, scope_key, key, stamp),
            )
        else:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries"
                f" WHERE scope = ? AND scope_key = ? AND {_CURRENT}"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT 100",
                (scope, scope_key, stamp),
            )
    return [_clean(r) for r in rows]


def list_all(scope: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Current facts, for the Memory panel. Optionally filtered to one scope."""
    stamp = _now()
    with get_conn() as conn:
        if scope:
            validate_scope(scope)
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries WHERE scope = ? AND {_CURRENT}"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT ?",
                (scope, stamp, limit),
            )
        else:
            rows = fetch_all(
                conn,
                f"SELECT {_FIELDS} FROM memory_entries WHERE {_CURRENT}"
                " ORDER BY updated_at DESC, entry_id DESC LIMIT ?",
                (stamp, limit),
            )
    return [_clean(r) for r in rows]


def candidates(scope: str = "", scope_key: str = "") -> list[dict[str, Any]]:
    """Current rows plus embeddings, for search to score in memory.

    Fetching and ranking in Python keeps one implementation across both
    backends. At personal scale this is trivially fast -- a few thousand
    384-float vectors score in milliseconds. Revisit only if the corpus grows
    past ~100k entries.
    """
    sql = f"SELECT {_FIELDS}, embedding FROM memory_entries WHERE {_CURRENT}"
    params: list[Any] = [_now()]
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
        if scope == "mission":
            sql += " AND scope_key = ?"
            params.append(scope_key)
    sql += " LIMIT 5000"

    with get_conn() as conn:
        rows = fetch_all(conn, sql, params)
    return rows


def stats() -> dict[str, Any]:
    stamp = _now()
    current = f"superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)"
    with get_conn() as conn:
        total = fetch_one(
            conn, f"SELECT COUNT(*) AS n FROM memory_entries WHERE {current}", (stamp,)
        )["n"]
        embedded = fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM memory_entries"
            f" WHERE embedding IS NOT NULL AND {current}",
            (stamp,),
        )["n"]
        by_scope = fetch_all(
            conn,
            f"SELECT scope, COUNT(*) AS n FROM memory_entries WHERE {current} GROUP BY scope",
            (stamp,),
        )
    return {
        "total": int(total),
        "embedded": int(embedded),
        "by_scope": {r["scope"]: int(r["n"]) for r in by_scope},
    }


def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    """Hard-delete and purge events. Values are not copied here."""
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT audit_id, entry_id, scope, scope_key, mem_key, action, created_at"
            " FROM memory_audit ORDER BY created_at DESC, audit_id DESC LIMIT ?",
            (limit,),
        )
    for row in rows:
        if row.get("created_at") is not None:
            row["created_at"] = str(row["created_at"])
    return rows


# --------------------------------------------------------------------------
# Deletion internals
# --------------------------------------------------------------------------


def _chain(conn: Any, entry_id: str) -> list[dict[str, Any]]:
    """The current row and every version linked to it, oldest link last."""
    head = entry_id
    seen: set[str] = set()
    while head and head not in seen:
        seen.add(head)
        row = fetch_one(
            conn,
            "SELECT entry_id, superseded_by FROM memory_entries WHERE entry_id = ?",
            (head,),
        )
        if not row or not row.get("superseded_by"):
            break
        head = row["superseded_by"]

    ordered: list[dict[str, Any]] = []
    cursor: str | None = head
    seen = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        row = fetch_one(
            conn,
            "SELECT entry_id, scope, scope_key, mem_key, supersedes FROM memory_entries"
            " WHERE entry_id = ?",
            (cursor,),
        )
        if not row:
            break
        ordered.append(row)
        cursor = row.get("supersedes") or None
    return ordered


def _delete_one(conn: Any, entry_id: str, action: str) -> bool:
    row = fetch_one(
        conn,
        "SELECT entry_id, scope, scope_key, mem_key FROM memory_entries WHERE entry_id = ?",
        (entry_id,),
    )
    if not row:
        return False
    audit_id = f"maud_{uuid.uuid4()}"
    execute(
        conn,
        """
        INSERT INTO memory_audit (audit_id, entry_id, scope, scope_key, mem_key, action, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id, row["entry_id"], row["scope"], row["scope_key"] or "",
            row["mem_key"], action, _now(),
        ),
    )
    execute(conn, "DELETE FROM memory_entries WHERE entry_id = ?", (entry_id,))
    return True


def _hard_delete(conn: Any, entry_id: str, action: str) -> int:
    removed = 0
    for row in _chain(conn, entry_id):
        if _delete_one(conn, row["entry_id"], action):
            removed += 1
    return removed
