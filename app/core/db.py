"""Dual-dialect database access: SQLite locally, Postgres in production.

The design doc's telemetry blueprint is written in raw SQL against SQLite.
Render's free tier has an ephemeral filesystem, so that SQLite file is wiped on
every redeploy, restart, and idle spin-down -- which would destroy exactly the
training data the whole telemetry engine exists to collect.

Rather than rewrite the storage layer later, every query goes through here.
Point DATABASE_URL at a Postgres instance (Neon's free tier needs no credit
card) and the same SQL runs against it -- no code change, one env var.

The only dialect differences that matter for our schema:
  * placeholders  -- sqlite3 uses `?`, psycopg uses `%s`     -> q()
  * type names    -- TIMESTAMP/JSON vs TIMESTAMPTZ/JSONB     -> _DDL_TYPES
  * boolean literals -- 1/0 vs TRUE/FALSE                    -> _DDL_TYPES
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.core.config import get_settings


def is_postgres() -> bool:
    return get_settings().storage_backend == "postgres"


def q(sql: str) -> str:
    """Translate `?` placeholders to `%s` when running on Postgres.

    Safe for our schema because none of the SQL contains a literal `?`.
    """
    return sql.replace("?", "%s") if is_postgres() else sql


# Type/literal names that differ between the two dialects. DDL is written with
# `{name}` slots and formatted with the right mapping at init time.
_DDL_TYPES: dict[str, dict[str, str]] = {
    "sqlite": {"ts": "TIMESTAMP", "now": "CURRENT_TIMESTAMP", "json": "TEXT", "true": "1", "false": "0"},
    "postgres": {"ts": "TIMESTAMPTZ", "now": "NOW()", "json": "JSONB", "true": "TRUE", "false": "FALSE"},
}


def ddl_types() -> dict[str, str]:
    return _DDL_TYPES["postgres" if is_postgres() else "sqlite"]


@contextmanager
def get_conn() -> Iterator[Any]:
    """Yield a connection, committing on success and rolling back on error."""
    settings = get_settings()

    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(settings.database_url, row_factory=dict_row)
    else:
        path = Path(settings.sqlite_path)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Concurrent reads while a write is in flight -- the WebSocket log
        # stream reads while a turn is being written.
        conn.execute("PRAGMA journal_mode=WAL")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> None:
    """Run a write. `sql` uses `?` placeholders regardless of backend."""
    conn.execute(q(sql), tuple(params))


def execute_script(conn: Any, script: str) -> None:
    """Run multi-statement DDL.

    sqlite3 has executescript(); psycopg's extended query protocol rejects
    multiple commands in one execute(), so statements are split and sent
    individually.
    """
    if is_postgres():
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)
    else:
        conn.executescript(script)


def fetch_all(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run a read, normalising both drivers' row types to plain dicts."""
    cur = conn.execute(q(sql), tuple(params))
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def fetch_one(conn: Any, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None
