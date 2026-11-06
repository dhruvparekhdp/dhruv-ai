"""Widen ``agy_channels.kind``. SQLite cannot drop a CHECK in place.

The rebuild follows SQLite's table-rebuild procedure. The migration runner
turns foreign keys off for the script that calls this, because content items
and calendar slots point at ``agy_channels`` and that procedure cannot drop
the table while enforcement is on. Postgres swaps the check instead.
"""

from __future__ import annotations

import re

from agency.template_seed import seed_channel_templates
from app.core.db import is_postgres

_OLD = (
    "blog",
    "newsletter",
    "linkedin",
    "instagram",
    "x",
    "youtube",
    "podcast",
    "other",
)
_NEW = (
    "blog",
    "newsletter",
    "linkedin",
    "instagram",
    "x",
    "youtube",
    "facebook",
    "website",
    "google_ads",
    "podcast",
    "other",
)
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _sql_list(values: tuple[str, ...]) -> str:
    rendered: list[str] = []
    for value in values:
        if _NAME.fullmatch(value) is None:
            raise RuntimeError(f"Refusing to interpolate channel kind {value!r}.")
        rendered.append(f"'{value}'")
    return ", ".join(rendered)


def widen_channel_kind(conn) -> None:
    if is_postgres():
        _swap_postgres(conn, _NEW)
        return
    _rebuild_sqlite(conn, _NEW)


def narrow_channel_kind(conn) -> None:
    """Restore the previous kind check. Callers fold new kinds to ``other`` first."""
    if is_postgres():
        _swap_postgres(conn, _OLD)
        return
    _rebuild_sqlite(conn, _OLD)


def _swap_postgres(conn, kinds: tuple[str, ...]) -> None:
    found = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) AS def "
        "FROM pg_constraint WHERE conrelid = 'agy_channels'::regclass AND contype = 'c'"
    ).fetchall()
    for row in found:
        name = row["conname"]
        definition = row["def"]
        if "kind" not in definition or _NAME.fullmatch(name) is None:
            continue
        conn.execute(f'ALTER TABLE agy_channels DROP CONSTRAINT "{name}"')
    conn.execute(
        "ALTER TABLE agy_channels ADD CONSTRAINT agy_channels_kind_check "
        f"CHECK (kind IN ({_sql_list(kinds)}))"
    )


def _rebuild_sqlite(conn, kinds: tuple[str, ...]) -> None:
    conn.execute("DROP TABLE IF EXISTS agy_channels__new")
    conn.execute(
        "CREATE TABLE agy_channels__new ("
        " id TEXT PRIMARY KEY,"
        " brand_id TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,"
        " name TEXT NOT NULL,"
        f" kind TEXT NOT NULL CHECK (kind IN ({_sql_list(kinds)})),"
        " handle TEXT NOT NULL DEFAULT '',"
        " notes TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO agy_channels__new ("
        " id, brand_id, name, kind, handle, notes, created_at, updated_at"
        ") SELECT id, brand_id, name, kind, handle, notes, created_at, updated_at"
        " FROM agy_channels"
    )
    conn.execute("DROP TABLE agy_channels")
    conn.execute("ALTER TABLE agy_channels__new RENAME TO agy_channels")
    conn.execute("CREATE INDEX idx_agy_channels_brand ON agy_channels (brand_id)")


HOOKS = {
    "widen_channel_kind": widen_channel_kind,
    "narrow_channel_kind": narrow_channel_kind,
    "seed_channel_templates": seed_channel_templates,
}
