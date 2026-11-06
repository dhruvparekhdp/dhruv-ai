"""Versioned schema migrations for the agency tables.

The rest of Jarvis creates tables with ``CREATE TABLE IF NOT EXISTS`` because
those schemas are still small and idempotent. Agency migrations have to go
up and down, so they are numbered SQL files plus a bookkeeping table.

DDL and the version row commit together. On SQLite that requires an explicit
``BEGIN``: without it a ``CREATE TABLE`` autocommits, and a crash before the
version insert would retry into "table already exists". Postgres already
wraps the connection in one transaction.

Run from the repo root:

    python -m agency.migrate up
    python -m agency.migrate down
    python -m agency.migrate down --steps 1
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agency.db import agency_conn
from app.core.db import execute, fetch_all, fetch_one, is_postgres

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_UP_NAME = re.compile(r"^(\d{4})_(.+)\.up\.sql$")
_PY_HOOK = re.compile(r"^-- migrate-python:([a-z_][a-z0-9_]*)$")
_BOOKKEEPING = "agy_schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path


def discover() -> list[Migration]:
    """Return migrations in version order. A missing down script is an error.

    Applying a change we cannot reverse would make ``down`` a lie, so this
    fails at discovery time rather than halfway through a rollback.
    """
    found: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.up.sql")):
        match = _UP_NAME.match(path.name)
        if match is None:
            raise RuntimeError(f"Migration file name not understood: {path.name}")
        version, name = match.group(1), match.group(2)
        down = path.with_name(f"{version}_{name}.down.sql")
        if not down.is_file():
            raise RuntimeError(f"Migration {version} ({name}) has no down script.")
        found.append(Migration(version=version, name=name, up_path=path, down_path=down))
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _statements(script: str) -> list[str]:
    """Split a migration script into statements.

    Comment lines are dropped first so a trailing note cannot become its own
    statement. The scripts do not contain semicolons inside strings.
    """
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    statements: list[str] = []
    for chunk in "\n".join(lines).split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements


def _table_exists(conn, name: str) -> bool:
    if is_postgres():
        row = fetch_one(
            conn,
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ?",
            (name,),
        )
    else:
        row = fetch_one(
            conn,
            "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        )
    return row is not None


def list_agency_tables() -> list[str]:
    """Names of ``agy_*`` tables currently in the database, sorted."""
    with agency_conn() as conn:
        if is_postgres():
            rows = fetch_all(
                conn,
                "SELECT tablename AS name FROM pg_tables "
                "WHERE schemaname = 'public' AND substr(tablename, 1, 4) = 'agy_' "
                "ORDER BY tablename",
            )
        else:
            rows = fetch_all(
                conn,
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND substr(name, 1, 4) = 'agy_' ORDER BY name",
            )
    return [row["name"] for row in rows]


def table_columns(table: str) -> list[str]:
    """Column names for one agency table, in catalog order."""
    if not table.startswith("agy_") or not table.replace("_", "").isalnum():
        raise ValueError(f"Refusing to inspect unexpected table {table!r}.")
    with agency_conn() as conn:
        if is_postgres():
            rows = fetch_all(
                conn,
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ? "
                "ORDER BY ordinal_position",
                (table,),
            )
        else:
            rows = fetch_all(conn, f"PRAGMA table_info({table})")
    return [row["name"] for row in rows]


def column_is_not_null(table: str, column: str) -> bool:
    """True when ``column`` exists on ``table`` and is declared NOT NULL.

    Table names are interpolated only after an exact ``agy_`` prefix check.
    They cannot be parameters of ``PRAGMA table_info``.
    """
    if not table.startswith("agy_") or not table.replace("_", "").isalnum():
        raise ValueError(f"Refusing to inspect unexpected table {table!r}.")
    if not column.replace("_", "").isalnum():
        raise ValueError(f"Refusing to inspect unexpected column {column!r}.")
    with agency_conn() as conn:
        if is_postgres():
            row = fetch_one(
                conn,
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ? AND column_name = ?",
                (table, column),
            )
            return row is not None and row["is_nullable"] == "NO"
        rows = fetch_all(conn, f"PRAGMA table_info({table})")
    for row in rows:
        if row["name"] == column:
            return bool(row["notnull"])
    return False


def _ensure_bookkeeping(conn) -> None:
    execute(
        conn,
        f"CREATE TABLE IF NOT EXISTS {_BOOKKEEPING} ("
        " version TEXT PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL"
        ")",
    )


def applied_versions() -> list[str]:
    """Applied migration versions, oldest first. Empty when nothing has run."""
    with agency_conn() as conn:
        if not _table_exists(conn, _BOOKKEEPING):
            return []
        rows = fetch_all(
            conn,
            f"SELECT version FROM {_BOOKKEEPING} ORDER BY version",
        )
    return [row["version"] for row in rows]


def _apply(conn, script: str) -> None:
    # SQLite autocommits DDL unless a transaction is already open. The version
    # row is inserted by the caller in this same transaction.
    if not is_postgres() and getattr(conn, "in_transaction", False) is False:
        conn.execute("BEGIN")
    buffer: list[str] = []

    def flush() -> None:
        kept = [line for line in buffer if not line.strip().startswith("--")]
        buffer.clear()
        for chunk in "\n".join(kept).split(";"):
            statement = chunk.strip()
            if statement:
                conn.execute(statement)

    for line in script.splitlines():
        match = _PY_HOOK.match(line.strip())
        if match is None:
            buffer.append(line)
            continue
        flush()
        from agency.channel_migrate import HOOKS as channel_hooks
        from agency.status_migrate import HOOKS as status_hooks

        hook = {**status_hooks, **channel_hooks}.get(match.group(1))
        if hook is None:
            raise RuntimeError(f"Unknown migration hook '{match.group(1)}'.")
        hook(conn)
    flush()


def _run_script(conn, script: str) -> None:
    """Apply one script. A ``sqlite-fk:off`` marker rebuilds tables safely.

    SQLite will not change ``foreign_keys`` inside a transaction, and the
    documented table rebuild refuses to drop a table that other rows point
    at while enforcement is on. The marker commits work that is already
    applied, turns enforcement off for this script, then commits this script
    with its bookkeeping change and turns enforcement back on.
    """
    relax = (not is_postgres()) and "sqlite-fk:off" in script
    if relax and getattr(conn, "in_transaction", False):
        conn.commit()
    if relax:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        _apply(conn, script)
    except Exception:
        if relax and getattr(conn, "in_transaction", False):
            conn.rollback()
        if relax:
            conn.execute("PRAGMA foreign_keys=ON")
        raise


def _finish_script(conn, script: str) -> None:
    if is_postgres() or "sqlite-fk:off" not in script:
        return
    if getattr(conn, "in_transaction", False):
        conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def migrate_up(target: str | None = None) -> list[str]:
    """Apply pending migrations. Returns the versions just applied.

    ``target`` stops after that version (inclusive). Already-applied versions
    are skipped, so calling this on boot is safe.
    """
    pending = discover()
    applied: list[str] = []
    with agency_conn() as conn:
        _ensure_bookkeeping(conn)
        done = {
            row["version"]
            for row in fetch_all(conn, f"SELECT version FROM {_BOOKKEEPING}")
        }
        for migration in pending:
            if migration.version in done:
                continue
            if target is not None and migration.version > target:
                break
            script = migration.up_path.read_text(encoding="utf-8")
            _run_script(conn, script)
            execute(
                conn,
                f"INSERT INTO {_BOOKKEEPING} (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, _now()),
            )
            _finish_script(conn, script)
            applied.append(migration.version)
    return applied


def migrate_down(steps: int | None = None) -> list[str]:
    """Reverse applied migrations, newest first.

    ``steps`` limits how many to reverse. The default reverses all of them
    and then drops the bookkeeping table, leaving no ``agy_`` tables behind.
    """
    if steps is not None and steps < 0:
        raise ValueError("steps must be zero or more.")
    by_version = {migration.version: migration for migration in discover()}
    reverted: list[str] = []
    with agency_conn() as conn:
        if not _table_exists(conn, _BOOKKEEPING):
            return []
        versions = [
            row["version"]
            for row in fetch_all(conn, f"SELECT version FROM {_BOOKKEEPING} ORDER BY version")
        ]
        targets = list(reversed(versions))
        if steps is not None:
            targets = targets[:steps]
        for version in targets:
            migration = by_version.get(version)
            if migration is None:
                raise RuntimeError(f"Applied migration {version} has no script on disk.")
            script = migration.down_path.read_text(encoding="utf-8")
            _run_script(conn, script)
            execute(conn, f"DELETE FROM {_BOOKKEEPING} WHERE version = ?", (version,))
            _finish_script(conn, script)
            reverted.append(version)
        remaining = fetch_all(conn, f"SELECT version FROM {_BOOKKEEPING}")
        if not remaining:
            execute(conn, f"DROP TABLE IF EXISTS {_BOOKKEEPING}")
    return reverted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agency.migrate")
    parser.add_argument("direction", choices=("up", "down"))
    parser.add_argument(
        "--steps",
        type=int,
        default=None,
        help="down only: how many migrations to reverse (default: all)",
    )
    args = parser.parse_args(argv)
    if args.direction == "up":
        if args.steps is not None:
            print("up does not take --steps", file=sys.stderr)
            return 2
        applied = migrate_up()
        print("applied: " + (", ".join(applied) if applied else "(none)"))
        return 0
    reverted = migrate_down(steps=args.steps)
    print("reverted: " + (", ".join(reverted) if reverted else "(none)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
