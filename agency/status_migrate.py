"""Replace the content-status check. SQLite cannot drop a CHECK in place.

The rebuild follows SQLite's table-rebuild procedure. The migration runner
turns foreign keys off for the script that calls this, because that procedure
cannot run while enforcement is on inside an open transaction. Postgres
swaps the named check instead of rebuilding. ``in_review`` from the previous
workflow becomes ``review``.
"""

from __future__ import annotations

import re

from app.core.db import is_postgres

_NEW_STATUSES = (
    "idea",
    "research",
    "brief",
    "outline",
    "draft",
    "creative_brief",
    "review",
    "approved",
    "scheduled",
    "published",
    "analyzed",
    "archived",
)
_OLD_STATUSES = ("draft", "in_review", "approved", "scheduled", "archived")
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _sql_list(values: tuple[str, ...]) -> str:
    rendered: list[str] = []
    for value in values:
        if _NAME.fullmatch(value) is None:
            raise RuntimeError(f"Refusing to interpolate status {value!r}.")
        rendered.append(f"'{value}'")
    return ", ".join(rendered)


def _check(values: tuple[str, ...], default: str) -> str:
    if _NAME.fullmatch(default) is None:
        raise RuntimeError(f"Refusing to interpolate default {default!r}.")
    return (
        f"status TEXT NOT NULL DEFAULT '{default}' "
        f"CHECK (status IN ({_sql_list(values)}))"
    )


def widen_content_status(conn) -> None:
    if is_postgres():
        _swap_postgres(conn, _NEW_STATUSES, "idea", map_legacy=True)
        return
    _rebuild_sqlite(conn, _NEW_STATUSES, "idea", map_legacy=True)


def narrow_content_status(conn) -> None:
    """Restore the pre-workflow check. Callers remap new labels to ``draft`` first."""
    if is_postgres():
        _swap_postgres(conn, _OLD_STATUSES, "draft", map_legacy=False)
        return
    _rebuild_sqlite(conn, _OLD_STATUSES, "draft", map_legacy=False)


def _swap_postgres(conn, statuses: tuple[str, ...], default: str, *, map_legacy: bool) -> None:
    found = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) AS def "
        "FROM pg_constraint WHERE conrelid = 'agy_content_items'::regclass AND contype = 'c'"
    ).fetchall()
    for row in found:
        name = row["conname"]
        definition = row["def"]
        if "status" not in definition or _NAME.fullmatch(name) is None:
            continue
        conn.execute(f'ALTER TABLE agy_content_items DROP CONSTRAINT "{name}"')
    if map_legacy:
        conn.execute("UPDATE agy_content_items SET status = 'review' WHERE status = 'in_review'")
    conn.execute(
        "ALTER TABLE agy_content_items ADD CONSTRAINT agy_content_items_status_check "
        f"CHECK (status IN ({_sql_list(statuses)}))"
    )
    conn.execute(f"ALTER TABLE agy_content_items ALTER COLUMN status SET DEFAULT '{default}'")


def _rebuild_sqlite(conn, statuses: tuple[str, ...], default: str, *, map_legacy: bool) -> None:
    status_sql = "CASE WHEN status = 'in_review' THEN 'review' ELSE status END" if map_legacy else "status"
    conn.execute("DROP TABLE IF EXISTS agy_content_items__new")
    conn.execute(
        "CREATE TABLE agy_content_items__new ("
        " id TEXT PRIMARY KEY,"
        " brand_id TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,"
        " idea_id TEXT REFERENCES agy_content_ideas(id) ON DELETE RESTRICT,"
        " channel_id TEXT REFERENCES agy_channels(id) ON DELETE RESTRICT,"
        " campaign_id TEXT REFERENCES agy_campaigns(id) ON DELETE RESTRICT,"
        " title TEXT NOT NULL,"
        " body TEXT NOT NULL DEFAULT '',"
        f" {_check(statuses, default)},"
        " scheduled_at TEXT,"
        " source_item_id TEXT REFERENCES agy_content_items(id) ON DELETE RESTRICT,"
        " published_url TEXT NOT NULL DEFAULT '',"
        " published_on TEXT,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO agy_content_items__new ("
        " id, brand_id, idea_id, channel_id, campaign_id, title, body, status,"
        " scheduled_at, source_item_id, published_url, published_on, created_at, updated_at"
        ") SELECT"
        " id, brand_id, idea_id, channel_id, campaign_id, title, body,"
        f" {status_sql},"
        " scheduled_at, source_item_id, published_url, published_on, created_at, updated_at"
        " FROM agy_content_items"
    )
    conn.execute("DROP TABLE agy_content_items")
    conn.execute("ALTER TABLE agy_content_items__new RENAME TO agy_content_items")
    conn.execute("CREATE INDEX idx_agy_items_brand ON agy_content_items (brand_id)")
    conn.execute("CREATE INDEX idx_agy_items_source ON agy_content_items (source_item_id)")


HOOKS = {
    "widen_content_status": widen_content_status,
    "narrow_content_status": narrow_content_status,
}
