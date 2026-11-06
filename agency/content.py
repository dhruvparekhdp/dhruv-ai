"""Brand-scoped content items.

A list or a search only returns the brand it was given. An update cannot
change ``brand_id``; the way to land a copy in another brand is
``duplicate_item``, which writes a new draft and does not carry approvals.

``schedule_item`` records a local schedule after checking that the channel
belongs to the same brand. It does not post, send, or publish anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agency.db import agency_conn
from agency.models import (
    NAME_MAX,
    TITLE_MAX,
    AgencyError,
    Channel,
    ChannelKind,
    ContentItem,
    ContentItemStatus,
    new_id,
)
from agency.scope import BrandScope, lookup
from agency.store import (
    _INTEGRITY,
    _bound,
    _clean_id,
    _coerce_enum,
    _now,
    _optional_text,
    _parse_ts,
    _raise_integrity,
    _require_text,
)

_BODY_MAX = 20_000
_SEARCH_MAX = 200
# Statuses a direct edit may set. ``scheduled`` only comes from schedule_item,
# which is where the channel brand is checked.
_EDITABLE_STATUSES = {
    ContentItemStatus.DRAFT,
    ContentItemStatus.IN_REVIEW,
    ContentItemStatus.ARCHIVED,
}


def _row_item(row: dict[str, Any]) -> ContentItem:
    return ContentItem(
        id=row["id"],
        brand_id=row["brand_id"],
        title=row["title"],
        idea_id=row.get("idea_id"),
        channel_id=row.get("channel_id"),
        campaign_id=row.get("campaign_id"),
        body=row.get("body") or "",
        status=ContentItemStatus(row["status"]),
        scheduled_at=_parse_ts(row.get("scheduled_at")),
        source_item_id=row.get("source_item_id"),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _row_channel(row: dict[str, Any]) -> Channel:
    return Channel(
        id=row["id"],
        brand_id=row["brand_id"],
        name=row["name"],
        kind=ChannelKind(row["kind"]),
        handle=row.get("handle") or "",
        notes=row.get("notes") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _like(value: str) -> str:
    """Literal substring match. ``%`` and ``_`` in the query are not wildcards."""
    escaped = (
        value.lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _iso(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.isoformat(timespec="microseconds")
    text = str(value or "").strip()
    if not text:
        raise AgencyError(f"{field} is required.")
    parsed = _parse_ts(text)
    if parsed is None:
        raise AgencyError(f"{field} must be an ISO-8601 timestamp.")
    return parsed.isoformat(timespec="microseconds")


def _same_brand(conn: Any, brand_id: str, table: str, record_id: str | None, field: str) -> str | None:
    if record_id is None:
        return None
    row = lookup(conn, table, record_id)
    if row is None:
        raise AgencyError(f"Unknown {field} '{record_id}'.")
    if row["brand_id"] != brand_id:
        raise AgencyError(f"{field} belongs to another brand.")
    return record_id


def create_channel(
    brand_id: str,
    name: str,
    kind: str | ChannelKind,
    *,
    handle: str = "",
    notes: str = "",
    channel_id: str | None = None,
) -> Channel:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            record_id = _clean_id(channel_id, "id", required=True) if channel_id else new_id("chn")
            assert record_id is not None
            now = _now()
            scope.insert(
                "agy_channels",
                {
                    "id": record_id,
                    "name": _require_text(name, "name", NAME_MAX),
                    "kind": _coerce_enum(kind, ChannelKind, "kind").value,
                    "handle": _optional_text(handle, "handle", 120),
                    "notes": _optional_text(notes, "notes", 4000),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_channels", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_channel(row)


def create_item(
    brand_id: str,
    title: str,
    *,
    body: str = "",
    status: str | ContentItemStatus = ContentItemStatus.DRAFT,
    channel_id: str | None = None,
    idea_id: str | None = None,
    campaign_id: str | None = None,
    item_id: str | None = None,
) -> ContentItem:
    cleaned_status = _coerce_enum(status, ContentItemStatus, "status")
    if cleaned_status is ContentItemStatus.SCHEDULED:
        raise AgencyError("Schedule an item through the schedule action so the channel brand is checked.")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            record_id = _clean_id(item_id, "id", required=True) if item_id else new_id("cit")
            assert record_id is not None
            channel = _same_brand(conn, scope.brand_id, "agy_channels", _clean_id(channel_id, "channel_id"), "channel_id")
            idea = _same_brand(conn, scope.brand_id, "agy_content_ideas", _clean_id(idea_id, "idea_id"), "idea_id")
            campaign = _same_brand(
                conn, scope.brand_id, "agy_campaigns", _clean_id(campaign_id, "campaign_id"), "campaign_id"
            )
            now = _now()
            scope.insert(
                "agy_content_items",
                {
                    "id": record_id,
                    "title": _require_text(title, "title", TITLE_MAX),
                    "body": _optional_text(body, "body", _BODY_MAX),
                    "status": cleaned_status.value,
                    "channel_id": channel,
                    "idea_id": idea,
                    "campaign_id": campaign,
                    "source_item_id": None,
                    "scheduled_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_content_items", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_item(row)


def get_item(brand_id: str, item_id: str) -> ContentItem | None:
    with agency_conn() as conn:
        row = BrandScope(conn, brand_id).get("agy_content_items", item_id)
    return _row_item(row) if row else None


def list_items(brand_id: str, *, limit: int = 100, offset: int = 0) -> list[ContentItem]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_content_items",
            order="updated_at DESC, id ASC",
            limit=limit_value,
            offset=offset_value,
        )
    return [_row_item(row) for row in rows]


def search_items(brand_id: str, query: str, *, limit: int = 50) -> list[ContentItem]:
    """Items in this brand whose title or body contains ``query``.

    Another brand's copy of the same words is not a result.
    """
    text = str(query or "").strip()
    if not text:
        raise AgencyError("A search query is required.")
    if len(text) > _SEARCH_MAX:
        raise AgencyError(f"Search query must be at most {_SEARCH_MAX} characters.")
    limit_value = _bound(limit, "limit", 1, 200)
    pattern = _like(text)
    where = "LOWER(title) LIKE ? ESCAPE '\\' OR LOWER(body) LIKE ? ESCAPE '\\'"
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_content_items",
            where=where,
            params=(pattern, pattern),
            order="updated_at DESC, id ASC",
            limit=limit_value,
        )
    return [_row_item(row) for row in rows]


def update_item(brand_id: str, item_id: str, /, **fields: Any) -> ContentItem | None:
    """Edit a draft inside its brand. ``brand_id`` in the payload is refused."""
    unknown = set(fields) - {"title", "body", "status", "brand_id", "channel_id", "source_item_id"}
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    if "brand_id" in fields or "channel_id" in fields or "source_item_id" in fields:
        raise AgencyError("Moving a record to another brand is not possible.")
    changes: dict[str, Any] = {}
    if "title" in fields:
        changes["title"] = _require_text(fields["title"], "title", TITLE_MAX)
    if "body" in fields:
        changes["body"] = _optional_text(fields["body"], "body", _BODY_MAX)
    if "status" in fields:
        status = _coerce_enum(fields["status"], ContentItemStatus, "status")
        if status not in _EDITABLE_STATUSES:
            raise AgencyError(
                "Schedule an item through the schedule action so the channel brand is checked."
            )
        changes["status"] = status.value
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if changes:
                changes["updated_at"] = _now()
            changed = scope.update("agy_content_items", item_id, changes)
            if changed == 0:
                return None
            row = scope.get("agy_content_items", item_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_item(row)


def duplicate_item(brand_id: str, item_id: str, target_brand_id: str) -> ContentItem | None:
    """Copy an item into ``target_brand_id`` as a new draft.

    The source stays where it is. The copy has a new id, ``source_item_id``
    pointing at the source, status ``draft``, no channel, and no approvals.
    """
    try:
        with agency_conn() as conn:
            source_scope = BrandScope(conn, brand_id)
            source = source_scope.get("agy_content_items", item_id)
            if source is None:
                return None
            target = BrandScope(conn, target_brand_id)
            record_id = new_id("cit")
            now = _now()
            target.insert(
                "agy_content_items",
                {
                    "id": record_id,
                    "title": source["title"],
                    "body": source.get("body") or "",
                    "status": ContentItemStatus.DRAFT.value,
                    "idea_id": None,
                    "channel_id": None,
                    "campaign_id": None,
                    "scheduled_at": None,
                    "source_item_id": source["id"],
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = target.get("agy_content_items", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_item(row)


def schedule_item(
    brand_id: str, item_id: str, channel_id: str, scheduled_at: Any
) -> ContentItem | None:
    """Mark an item scheduled on a channel of the same brand.

    This writes the row on this host. It does not publish the item, send it,
    or call the channel.
    """
    when = _iso(scheduled_at, "scheduled_at")
    cleaned_channel = _clean_id(channel_id, "channel_id", required=True)
    assert cleaned_channel is not None
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            item = scope.get("agy_content_items", item_id)
            if item is None:
                return None
            channel = lookup(conn, "agy_channels", cleaned_channel)
            if channel is None:
                raise AgencyError(f"Unknown channel_id '{cleaned_channel}'.")
            if channel["brand_id"] != item["brand_id"] or channel["brand_id"] != scope.brand_id:
                raise AgencyError("Cannot schedule an item onto a channel from another brand.")
            scope.update(
                "agy_content_items",
                item_id,
                {
                    "channel_id": cleaned_channel,
                    "status": ContentItemStatus.SCHEDULED.value,
                    "scheduled_at": when,
                    "updated_at": _now(),
                },
            )
            row = scope.get("agy_content_items", item_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_item(row)


def record_approval(brand_id: str, item_id: str, *, note: str = "") -> str:
    """Record an approval decision on an item. Used to prove a duplicate does not inherit it."""
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_content_items", item_id) is None:
                raise AgencyError("Unknown content item.")
            approval_id = new_id("apv")
            now = _now()
            scope.insert(
                "agy_approvals",
                {
                    "id": approval_id,
                    "subject_type": "content_item",
                    "subject_id": item_id,
                    "status": "approved",
                    "note": _optional_text(note, "note", 4000),
                    "decided_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    return approval_id


def approvals_for_item(brand_id: str, item_id: str) -> list[dict[str, Any]]:
    with agency_conn() as conn:
        return BrandScope(conn, brand_id).select(
            "agy_approvals",
            where="subject_type = ? AND subject_id = ?",
            params=("content_item", item_id),
            order="created_at ASC",
        )
