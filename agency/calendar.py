"""Brand calendar slots.

A slot is a planning row. Creating or moving one does not change a content
item's workflow status, does not publish, and does not call the channel.
Two live slots for the same brand, channel and start time are both kept. The
response names the others so the owner can see the clash. Cancelled slots do
not take part. A different start time is not a clash, even when the ranges
overlap: the warning is the instant, not the window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agency.db import agency_conn
from agency.models import (
    TITLE_MAX,
    AgencyError,
    CalendarSlot,
    CalendarSlotStatus,
    new_id,
)
from agency.scope import BrandScope, lookup
from agency.secrets import reject_secrets
from agency.store import (
    _INTEGRITY,
    _clean_id,
    _coerce_enum,
    _now,
    _optional_text,
    _parse_ts,
    _raise_integrity,
)

_RANGE_CAP = 500
_KEEP = object()


def _timestamp(value: Any, field: str) -> str:
    """UTC ISO text with microseconds, so a clash is the same instant.

    Naive values are UTC. The column is text, and both backends store the
    string we write, so the comparison does not depend on a native timestamp.
    """
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat(timespec="microseconds")
    text = str(value or "").strip()
    if not text:
        raise AgencyError(f"{field} is required.")
    parsed = _parse_ts(text)
    if parsed is None:
        raise AgencyError(f"{field} must be an ISO-8601 timestamp.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _row(row: dict[str, Any]) -> CalendarSlot:
    starts = _parse_ts(row.get("starts_at"))
    if starts is None:
        raise AgencyError("starts_at is required.")
    return CalendarSlot(
        id=row["id"],
        brand_id=row["brand_id"],
        starts_at=starts,
        channel_id=row.get("channel_id"),
        content_item_id=row.get("content_item_id"),
        title=row.get("title") or "",
        status=CalendarSlotStatus(row["status"]),
        ends_at=_parse_ts(row.get("ends_at")),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _same_brand(conn: Any, brand_id: str, table: str, record_id: str | None, field: str) -> str | None:
    if record_id is None:
        return None
    row = lookup(conn, table, record_id)
    if row is None:
        raise AgencyError(f"Unknown {field} '{record_id}'.")
    if row["brand_id"] != brand_id:
        raise AgencyError(f"{field} belongs to another brand.")
    return record_id


def _conflicts(
    scope: BrandScope,
    channel_id: str | None,
    starts_at: str,
    except_id: str | None = None,
) -> list[CalendarSlot]:
    if not channel_id:
        return []
    where = "channel_id = ? AND starts_at = ? AND status != 'cancelled'"
    params: list[Any] = [channel_id, starts_at]
    if except_id:
        where += " AND id != ?"
        params.append(except_id)
    rows = scope.select(
        "agy_calendar_slots",
        where=where,
        params=tuple(params),
        order="id ASC",
    )
    return [_row(row) for row in rows]


def warning(slot: CalendarSlot) -> dict[str, Any]:
    return {
        "id": slot.id,
        "title": slot.title,
        "channel_id": slot.channel_id,
        "starts_at": slot.starts_at.isoformat(),
    }


def list_slots(
    brand_id: str,
    *,
    channel_id: str | None = None,
    starts_from: Any,
    starts_to: Any,
) -> list[dict[str, Any]]:
    """Slots whose start falls inside the range, inclusive, oldest first."""
    start = _timestamp(starts_from, "starts_from")
    end = _timestamp(starts_to, "starts_to")
    if start > end:
        raise AgencyError("starts_from must be at or before starts_to.")
    channel = _clean_id(channel_id, "channel_id") if channel_id else None
    with agency_conn() as conn:
        scope = BrandScope(conn, brand_id)
        if channel is not None:
            _same_brand(conn, scope.brand_id, "agy_channels", channel, "channel_id")
        where = "starts_at >= ? AND starts_at <= ?"
        params: list[Any] = [start, end]
        if channel is not None:
            where += " AND channel_id = ?"
            params.append(channel)
        rows = scope.select(
            "agy_calendar_slots",
            where=where,
            params=tuple(params),
            order="starts_at ASC, id ASC",
            limit=_RANGE_CAP + 1,
        )
    if len(rows) > _RANGE_CAP:
        raise AgencyError(f"That range has more than {_RANGE_CAP} slots. Narrow the dates.")
    slots = [_row(row) for row in rows]
    groups: dict[tuple[str, datetime], list[str]] = {}
    for slot in slots:
        if slot.status is CalendarSlotStatus.CANCELLED or not slot.channel_id:
            continue
        groups.setdefault((slot.channel_id, slot.starts_at), []).append(slot.id)
    flagged = {slot_id for ids in groups.values() if len(ids) > 1 for slot_id in ids}
    listed: list[dict[str, Any]] = []
    for slot in slots:
        payload = slot.to_dict()
        payload["conflict"] = slot.id in flagged
        payload["conflict_ids"] = [
            slot_id
            for slot_id in groups.get((slot.channel_id or "", slot.starts_at), [])
            if slot_id != slot.id and slot.id in flagged
        ]
        listed.append(payload)
    return listed


def create_slot(
    brand_id: str,
    starts_at: Any,
    *,
    channel_id: str | None = None,
    content_item_id: str | None = None,
    title: str = "",
    status: str | CalendarSlotStatus = CalendarSlotStatus.OPEN,
    ends_at: Any = None,
    slot_id: str | None = None,
) -> tuple[CalendarSlot, list[CalendarSlot]]:
    """Insert a slot and report other live slots at the same channel and time."""
    when = _timestamp(starts_at, "starts_at")
    ending = _timestamp(ends_at, "ends_at") if ends_at is not None else None
    if ending is not None and ending < when:
        raise AgencyError("ends_at must be at or after starts_at.")
    label = _optional_text(title, "title", TITLE_MAX)
    reject_secrets(label, "title")
    state = _coerce_enum(status, CalendarSlotStatus, "status")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            record_id = _clean_id(slot_id, "id", required=True) if slot_id else new_id("slt")
            assert record_id is not None
            channel = _same_brand(
                conn, scope.brand_id, "agy_channels", _clean_id(channel_id, "channel_id"), "channel_id"
            )
            item = _same_brand(
                conn,
                scope.brand_id,
                "agy_content_items",
                _clean_id(content_item_id, "content_item_id"),
                "content_item_id",
            )
            clashes = (
                _conflicts(scope, channel, when)
                if state is not CalendarSlotStatus.CANCELLED
                else []
            )
            now = _now()
            scope.insert(
                "agy_calendar_slots",
                {
                    "id": record_id,
                    "channel_id": channel,
                    "content_item_id": item,
                    "title": label,
                    "status": state.value,
                    "starts_at": when,
                    "ends_at": ending,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_calendar_slots", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row(row), clashes


def reschedule(
    brand_id: str,
    slot_id: str,
    starts_at: Any,
    *,
    ends_at: Any = _KEEP,
) -> tuple[CalendarSlot, list[CalendarSlot]] | None:
    """Move a slot. ``None`` when it is not in this brand. The channel stays."""
    cleaned = _clean_id(slot_id, "slot_id", required=True)
    assert cleaned is not None
    when = _timestamp(starts_at, "starts_at")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = scope.get("agy_calendar_slots", cleaned)
            if current is None:
                return None
            if ends_at is None:
                ending = None
            elif ends_at is _KEEP:
                raw_end = current.get("ends_at")
                ending = _timestamp(raw_end, "ends_at") if raw_end else None
            else:
                ending = _timestamp(ends_at, "ends_at")
            if ending is not None and ending < when:
                raise AgencyError("ends_at must be at or after starts_at.")
            state = CalendarSlotStatus(current["status"])
            clashes = (
                _conflicts(scope, current.get("channel_id"), when, except_id=cleaned)
                if state is not CalendarSlotStatus.CANCELLED
                else []
            )
            scope.update(
                "agy_calendar_slots",
                cleaned,
                {"starts_at": when, "ends_at": ending, "updated_at": _now()},
            )
            row = scope.get("agy_calendar_slots", cleaned)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row(row), clashes
