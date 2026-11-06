"""Content workflow: transitions, revisions, and approvers.

Status changes go through ``transition``. The allowed step is the next one in
the chain, plus archive, plus restoring an archived item to idea. Entering
``approved`` requires the latest decision on the item to be an approval from
an approver of that brand. ``published`` stores a URL and a date on this host.
It does not call the platform.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from typing import Any

from agency.db import agency_conn
from agency.models import (
    NAME_MAX,
    NOTES_MAX,
    URL_MAX,
    AgencyConflict,
    AgencyError,
    Approval,
    ApprovalStatus,
    ApprovalSubject,
    Approver,
    ContentItem,
    ContentItemStatus,
    ContentRevision,
    new_id,
)
from agency.scope import BrandScope, lookup
from agency.store import (
    _INTEGRITY,
    _bound,
    _clean_email,
    _clean_id,
    _coerce_enum,
    _now,
    _optional_text,
    _parse_ts,
    _raise_integrity,
    _require_text,
)

_BODY_MAX = 20_000

# The chain in the ticket. Archive is a side door. Archived items return to idea.
FORWARD: tuple[ContentItemStatus, ...] = (
    ContentItemStatus.IDEA,
    ContentItemStatus.RESEARCH,
    ContentItemStatus.BRIEF,
    ContentItemStatus.OUTLINE,
    ContentItemStatus.DRAFT,
    ContentItemStatus.CREATIVE_BRIEF,
    ContentItemStatus.REVIEW,
    ContentItemStatus.APPROVED,
    ContentItemStatus.SCHEDULED,
    ContentItemStatus.PUBLISHED,
    ContentItemStatus.ANALYZED,
)


def _transitions() -> dict[ContentItemStatus, frozenset[ContentItemStatus]]:
    allowed: dict[ContentItemStatus, set[ContentItemStatus]] = {
        status: set() for status in ContentItemStatus
    }
    for current, nxt in itertools.pairwise(FORWARD):
        allowed[current].add(nxt)
    for status in ContentItemStatus:
        if status is not ContentItemStatus.ARCHIVED:
            allowed[status].add(ContentItemStatus.ARCHIVED)
    allowed[ContentItemStatus.ARCHIVED].add(ContentItemStatus.IDEA)
    return {status: frozenset(targets) for status, targets in allowed.items()}


TRANSITIONS = _transitions()

_APPROVAL_REQUIRED = (
    "Entering approved requires an approval from an allowed approver for this brand."
)


def to_item(row: dict[str, Any]) -> ContentItem:
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
        published_url=row.get("published_url") or "",
        published_on=_published_day(row.get("published_on")),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _published_day(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _row_revision(row: dict[str, Any]) -> ContentRevision:
    return ContentRevision(
        id=row["id"],
        brand_id=row["brand_id"],
        content_item_id=row["content_item_id"],
        revision_no=int(row["revision_no"]),
        author=row.get("author") or "",
        title=row.get("title") or "",
        body=row.get("body") or "",
        note=row.get("note") or "",
        created_at=_parse_ts(row.get("created_at")),
    )


def _row_approver(row: dict[str, Any]) -> Approver:
    return Approver(
        id=row["id"],
        brand_id=row["brand_id"],
        name=row["name"],
        email=row.get("email") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _row_approval(row: dict[str, Any]) -> Approval:
    return Approval(
        id=row["id"],
        brand_id=row["brand_id"],
        subject_type=ApprovalSubject(row["subject_type"]),
        subject_id=row["subject_id"],
        status=ApprovalStatus(row["status"]),
        approver_id=row.get("approver_id"),
        note=row.get("note") or "",
        decided_at=_parse_ts(row.get("decided_at")),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def add_revision(
    scope: BrandScope,
    *,
    item_id: str,
    title: str,
    body: str,
    author: str,
    note: str = "",
) -> None:
    """Append a full title/body snapshot. Two snapshots are what a diff compares."""
    author_text = _require_text(author, "author", NAME_MAX)
    rows = scope.select(
        "agy_content_revisions",
        where="content_item_id = ?",
        params=(item_id,),
        order="revision_no DESC",
        limit=1,
    )
    number = 1 if not rows else int(rows[0]["revision_no"]) + 1
    scope.insert(
        "agy_content_revisions",
        {
            "id": new_id("rev"),
            "content_item_id": item_id,
            "revision_no": number,
            "title": title,
            "body": body or "",
            "author": author_text,
            "note": _optional_text(note, "note", NOTES_MAX),
            "created_at": _now(),
        },
    )


def list_revisions(brand_id: str, item_id: str) -> list[ContentRevision] | None:
    """Revisions for one item, oldest first. ``None`` when the item is not in the brand."""
    with agency_conn() as conn:
        scope = BrandScope(conn, brand_id)
        if scope.get("agy_content_items", item_id) is None:
            return None
        rows = scope.select(
            "agy_content_revisions",
            where="content_item_id = ?",
            params=(item_id,),
            order="revision_no ASC",
        )
    return [_row_revision(row) for row in rows]


def create_approver(brand_id: str, name: str, *, email: str = "") -> Approver:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            record_id = new_id("apr")
            now = _now()
            scope.insert(
                "agy_approvers",
                {
                    "id": record_id,
                    "name": _require_text(name, "name", NAME_MAX),
                    "email": _clean_email(email),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_approvers", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_approver(row)


def list_approvers(brand_id: str, *, limit: int = 100, offset: int = 0) -> list[Approver]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_approvers",
            order="name ASC, id ASC",
            limit=limit_value,
            offset=offset_value,
        )
    return [_row_approver(row) for row in rows]


def delete_approver(brand_id: str, approver_id: str) -> bool:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_approvers", approver_id) is None:
                return False
            scope.delete("agy_approvers", approver_id)
    except _INTEGRITY as exc:
        raise AgencyConflict(
            "Approver is still referenced. Remove the decisions that point at them first."
        ) from exc
    return True


def record_decision(
    brand_id: str,
    item_id: str,
    approver_id: str,
    status: str | ApprovalStatus,
    *,
    note: str = "",
) -> Approval | None:
    """Record a decision by an approver of this brand. ``None`` if the item is absent."""
    cleaned = _coerce_enum(status, ApprovalStatus, "status")
    approver_key = _clean_id(approver_id, "approver_id", required=True)
    assert approver_key is not None
    note_text = _optional_text(note, "note", NOTES_MAX)
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_content_items", item_id) is None:
                return None
            if scope.get("agy_approvers", approver_key) is None:
                raise AgencyError("Unknown approver for this brand.")
            record_id = new_id("apv")
            now = _now()
            scope.insert(
                "agy_approvals",
                {
                    "id": record_id,
                    "subject_type": ApprovalSubject.CONTENT_ITEM.value,
                    "subject_id": item_id,
                    "status": cleaned.value,
                    "approver_id": approver_key,
                    "note": note_text,
                    "decided_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_approvals", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_approval(row)


def _timestamp(value: Any, field: str) -> str:
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


def _http_url(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > URL_MAX or any(character.isspace() for character in text):
        raise AgencyError("published_url must be an http(s) URL.")
    if not text.startswith(("https://", "http://")):
        raise AgencyError("published_url must be an http(s) URL.")
    return text


def _day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise AgencyError("published_on is required.")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise AgencyError("published_on must be a date.") from exc


def _refuse(current: ContentItemStatus, dest: ContentItemStatus) -> None:
    raise AgencyError(f"Cannot move from {current.value} to {dest.value}.")


def _approval_allows(scope: BrandScope, item_id: str) -> None:
    rows = scope.select(
        "agy_approvals",
        where="subject_type = ? AND subject_id = ?",
        params=(ApprovalSubject.CONTENT_ITEM.value, item_id),
        order="created_at DESC, id DESC",
        limit=1,
    )
    if not rows:
        raise AgencyError(_APPROVAL_REQUIRED)
    latest = rows[0]
    approver_id = latest.get("approver_id")
    if latest["status"] != ApprovalStatus.APPROVED.value or not approver_id:
        raise AgencyError(_APPROVAL_REQUIRED)
    if scope.get("agy_approvers", approver_id) is None:
        raise AgencyError(_APPROVAL_REQUIRED)


def _channel_for_schedule(conn: Any, scope: BrandScope, item: dict[str, Any], channel_id: str) -> str:
    cleaned = _clean_id(channel_id, "channel_id", required=True)
    assert cleaned is not None
    channel = lookup(conn, "agy_channels", cleaned)
    if channel is None:
        raise AgencyError(f"Unknown channel_id '{cleaned}'.")
    if channel["brand_id"] != item["brand_id"] or channel["brand_id"] != scope.brand_id:
        raise AgencyError("Cannot schedule an item onto a channel from another brand.")
    return cleaned


def transition(
    brand_id: str,
    item_id: str,
    status: str | ContentItemStatus,
    *,
    author: str,
    note: str = "",
    channel_id: str | None = None,
    scheduled_at: Any = None,
    published_url: str = "",
    published_on: Any = None,
) -> dict[str, Any] | None:
    """Move one item. ``None`` when the item is not in this brand.

    A cross-brand channel is refused before the status graph, so a schedule
    aimed at another brand's channel is rejected even when the step itself
    is not allowed yet.
    """
    dest = _coerce_enum(status, ContentItemStatus, "status")
    author_text = _require_text(author, "author", NAME_MAX)
    note_text = _optional_text(note, "note", NOTES_MAX)
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            item = scope.get("agy_content_items", item_id)
            if item is None:
                return None
            current = ContentItemStatus(item["status"])
            scheduled_channel: str | None = None
            scheduled_when: str | None = None
            if dest is ContentItemStatus.SCHEDULED:
                if not channel_id or scheduled_at is None:
                    if dest not in TRANSITIONS[current]:
                        _refuse(current, dest)
                    raise AgencyError("Scheduling requires a channel and a time.")
                scheduled_channel = _channel_for_schedule(conn, scope, item, channel_id)
                scheduled_when = _timestamp(scheduled_at, "scheduled_at")
            if dest not in TRANSITIONS[current]:
                _refuse(current, dest)
            publish_url = ""
            publish_day = ""
            if dest is ContentItemStatus.APPROVED:
                _approval_allows(scope, item_id)
            if dest is ContentItemStatus.PUBLISHED:
                publish_url = _http_url(published_url)
                publish_day = _day(published_on)
            changes: dict[str, Any] = {"status": dest.value, "updated_at": _now()}
            if dest is ContentItemStatus.SCHEDULED:
                changes["channel_id"] = scheduled_channel
                changes["scheduled_at"] = scheduled_when
            if dest is ContentItemStatus.PUBLISHED:
                changes["published_url"] = publish_url
                changes["published_on"] = publish_day
            scope.update("agy_content_items", item_id, changes)
            add_revision(
                scope,
                item_id=item_id,
                title=item["title"],
                body=item.get("body") or "",
                author=author_text,
                note=note_text or f"status: {current.value} -> {dest.value}",
            )
            row = scope.get("agy_content_items", item_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return row
