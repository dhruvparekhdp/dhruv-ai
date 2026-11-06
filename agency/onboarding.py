"""Brand profile, access checklist, and the onboarding wizard.

Every read and write goes through ``BrandScope``. A profile belongs to one
brand. This module does not move a profile onto another brand.

The checklist is status and notes. Nothing here publishes, sends, or stores
a password. ``services_bought`` is a name the owner typed, not an order.
"""

from __future__ import annotations

import json
from typing import Any

from agency.db import agency_conn
from agency.models import (
    NAME_MAX,
    NOTES_MAX,
    PROFILE_LIST_MAX,
    ROLE_MAX,
    SAMPLE_POST_MAX,
    URL_MAX,
    WORD_MAX,
    AccessItem,
    AccessPlatform,
    AccessStatus,
    AgencyConflict,
    AgencyError,
    BrandProfile,
    ChannelKind,
    new_id,
)
from agency.scope import BrandScope
from agency.secrets import reject_secrets
from agency.store import (
    _INTEGRITY,
    _bound,
    _clean_email,
    _coerce_enum,
    _now,
    _optional_text,
    _parse_ts,
    _raise_integrity,
    _require_text,
)

_TEXT_FIELDS = ("goals", "audience", "positioning", "voice_description")
_WORD_FIELDS = ("words_to_use", "words_to_avoid")
_OBJECT_FIELDS = ("competitors", "active_channels", "approval_contacts", "services_bought")
_PROFILE_FIELDS = _TEXT_FIELDS + _WORD_FIELDS + ("sample_posts",) + _OBJECT_FIELDS
_JSON_FIELDS = _WORD_FIELDS + ("sample_posts",) + _OBJECT_FIELDS

# A requested row is still outstanding. Granted and not-needed are answers.
_SETTLED = {AccessStatus.GRANTED.value, AccessStatus.NOT_NEEDED.value}

_STEPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("basics", "Goals, audience, and positioning", ("goals", "audience", "positioning")),
    (
        "voice",
        "Voice description and word lists",
        ("voice_description", "words_to_use", "words_to_avoid"),
    ),
    ("samples", "Sample posts", ("sample_posts",)),
    ("market", "Competitors and active channels", ("competitors", "active_channels")),
    ("approvals", "Approval contacts", ("approval_contacts",)),
    ("services", "Services bought", ("services_bought",)),
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_list(raw: Any, field: str) -> list[Any]:
    if raw is None or raw == "":
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgencyError(f"{field} on this profile cannot be read.") from exc
    if not isinstance(loaded, list):
        raise AgencyError(f"{field} on this profile cannot be read.")
    return loaded


def _plain(item: Any) -> Any:
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump()
    return item


def _string_list(value: Any, field: str, *, item_max: int) -> list[str]:
    if not isinstance(value, list):
        raise AgencyError(f"{field} must be a list.")
    if len(value) > PROFILE_LIST_MAX:
        raise AgencyError(f"{field} must contain at most {PROFILE_LIST_MAX} items.")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AgencyError(f"{field} entries must be text.")
        text = item.strip()
        if not text:
            raise AgencyError(f"{field} cannot contain a blank item.")
        if len(text) > item_max:
            raise AgencyError(f"{field} entries must be at most {item_max} characters.")
        reject_secrets(text, field)
        cleaned.append(text)
    return cleaned


def _object(item: Any, field: str) -> dict[str, Any]:
    plain = _plain(item)
    if not isinstance(plain, dict):
        raise AgencyError(f"{field} entries must be objects.")
    return plain


def _competitors(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise AgencyError("competitors must be a list.")
    if len(value) > PROFILE_LIST_MAX:
        raise AgencyError(f"competitors must contain at most {PROFILE_LIST_MAX} items.")
    cleaned: list[dict[str, str]] = []
    for item in value:
        raw = _object(item, "competitors")
        unknown = set(raw) - {"name", "website", "notes"}
        if unknown:
            raise AgencyError(f"Unknown field(s) on competitors: {', '.join(sorted(unknown))}.")
        entry = {
            "name": _require_text(raw.get("name"), "competitors.name", NAME_MAX),
            "website": _optional_text(raw.get("website", ""), "competitors.website", URL_MAX),
            "notes": _optional_text(raw.get("notes", ""), "competitors.notes", NOTES_MAX),
        }
        reject_secrets(entry, "competitors")
        cleaned.append(entry)
    return cleaned


def _channels(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise AgencyError("active_channels must be a list.")
    if len(value) > PROFILE_LIST_MAX:
        raise AgencyError(f"active_channels must contain at most {PROFILE_LIST_MAX} items.")
    cleaned: list[dict[str, str]] = []
    for item in value:
        raw = _object(item, "active_channels")
        unknown = set(raw) - {"name", "kind"}
        if unknown:
            raise AgencyError(f"Unknown field(s) on active_channels: {', '.join(sorted(unknown))}.")
        entry = {
            "name": _require_text(raw.get("name"), "active_channels.name", NAME_MAX),
            "kind": _coerce_enum(raw.get("kind"), ChannelKind, "active_channels.kind").value,
        }
        reject_secrets(entry, "active_channels")
        cleaned.append(entry)
    return cleaned


def _contacts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise AgencyError("approval_contacts must be a list.")
    if len(value) > PROFILE_LIST_MAX:
        raise AgencyError(f"approval_contacts must contain at most {PROFILE_LIST_MAX} items.")
    cleaned: list[dict[str, str]] = []
    for item in value:
        raw = _object(item, "approval_contacts")
        unknown = set(raw) - {"name", "email", "role"}
        if unknown:
            raise AgencyError(
                f"Unknown field(s) on approval_contacts: {', '.join(sorted(unknown))}."
            )
        entry = {
            "name": _require_text(raw.get("name"), "approval_contacts.name", NAME_MAX),
            "email": _clean_email(raw.get("email", "")),
            "role": _optional_text(raw.get("role", ""), "approval_contacts.role", ROLE_MAX),
        }
        reject_secrets(entry, "approval_contacts")
        cleaned.append(entry)
    return cleaned


def _services(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise AgencyError("services_bought must be a list.")
    if len(value) > PROFILE_LIST_MAX:
        raise AgencyError(f"services_bought must contain at most {PROFILE_LIST_MAX} items.")
    cleaned: list[dict[str, str]] = []
    for item in value:
        raw = _object(item, "services_bought")
        unknown = set(raw) - {"name", "notes"}
        if unknown:
            raise AgencyError(f"Unknown field(s) on services_bought: {', '.join(sorted(unknown))}.")
        entry = {
            "name": _require_text(raw.get("name"), "services_bought.name", NAME_MAX),
            "notes": _optional_text(raw.get("notes", ""), "services_bought.notes", NOTES_MAX),
        }
        reject_secrets(entry, "services_bought")
        cleaned.append(entry)
    return cleaned


def _clean_profile(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate one full profile payload and return column values."""
    unknown = set(fields) - set(_PROFILE_FIELDS)
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    cleaned: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        text = _optional_text(fields.get(name, ""), name, NOTES_MAX)
        reject_secrets(text, name)
        cleaned[name] = text
    for name in _WORD_FIELDS:
        cleaned[name] = _string_list(fields.get(name, []), name, item_max=WORD_MAX)
    cleaned["sample_posts"] = _string_list(
        fields.get("sample_posts", []), "sample_posts", item_max=SAMPLE_POST_MAX
    )
    cleaned["competitors"] = _competitors(fields.get("competitors", []))
    cleaned["active_channels"] = _channels(fields.get("active_channels", []))
    cleaned["approval_contacts"] = _contacts(fields.get("approval_contacts", []))
    cleaned["services_bought"] = _services(fields.get("services_bought", []))
    return cleaned


def _empty_profile() -> dict[str, Any]:
    return _clean_profile({})


def _stored_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a database row back into the shape ``_clean_profile`` accepts."""
    payload: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        payload[name] = row.get(name) or ""
    for name in _JSON_FIELDS:
        payload[name] = _load_list(row.get(name), name)
    return payload


def _columns(cleaned: dict[str, Any]) -> dict[str, Any]:
    columns = dict(cleaned)
    for name in _JSON_FIELDS:
        columns[name] = _dump(cleaned[name])
    return columns


def _row_profile(row: dict[str, Any]) -> BrandProfile:
    loaded = _stored_profile(row)
    return BrandProfile(
        id=row["id"],
        brand_id=row["brand_id"],
        goals=loaded["goals"],
        audience=loaded["audience"],
        positioning=loaded["positioning"],
        voice_description=loaded["voice_description"],
        words_to_use=list(loaded["words_to_use"]),
        words_to_avoid=list(loaded["words_to_avoid"]),
        sample_posts=list(loaded["sample_posts"]),
        competitors=list(loaded["competitors"]),
        active_channels=list(loaded["active_channels"]),
        approval_contacts=list(loaded["approval_contacts"]),
        services_bought=list(loaded["services_bought"]),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _row_access(row: dict[str, Any]) -> AccessItem:
    return AccessItem(
        id=row["id"],
        brand_id=row["brand_id"],
        platform=AccessPlatform(row["platform"]),
        status=AccessStatus(row["status"]),
        notes=row.get("notes") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _one_profile(scope: BrandScope) -> dict[str, Any] | None:
    rows = scope.select("agy_brand_profiles", order="id ASC", limit=1)
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def create_profile(brand_id: str, **fields: Any) -> BrandProfile:
    reject_secrets(fields, "profile")
    cleaned = _clean_profile(fields)
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if _one_profile(scope) is not None:
                raise AgencyConflict("This brand already has a profile.")
            record_id = new_id("prf")
            now = _now()
            scope.insert(
                "agy_brand_profiles",
                {"id": record_id, "created_at": now, "updated_at": now, **_columns(cleaned)},
            )
            row = scope.get("agy_brand_profiles", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_profile(row)


def get_profile(brand_id: str) -> BrandProfile | None:
    with agency_conn() as conn:
        row = _one_profile(BrandScope(conn, brand_id))
    return _row_profile(row) if row else None


def update_profile(brand_id: str, /, **fields: Any) -> BrandProfile | None:
    if "brand_id" in fields:
        raise AgencyError("Moving a record to another brand is not possible.")
    reject_secrets(fields, "profile")
    if not fields:
        return get_profile(brand_id)
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = _one_profile(scope)
            if current is None:
                return None
            merged = _stored_profile(current)
            merged.update(fields)
            cleaned = _clean_profile(merged)
            scope.update(
                "agy_brand_profiles",
                current["id"],
                {**_columns(cleaned), "updated_at": _now()},
            )
            row = scope.get("agy_brand_profiles", current["id"])
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_profile(row)


def delete_profile(brand_id: str) -> bool:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = _one_profile(scope)
            if current is None:
                return False
            scope.delete("agy_brand_profiles", current["id"])
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    return True


# --------------------------------------------------------------------------
# Access checklist
# --------------------------------------------------------------------------


def create_access(
    brand_id: str,
    platform: str | AccessPlatform,
    *,
    status: str | AccessStatus = AccessStatus.REQUESTED,
    notes: str = "",
) -> AccessItem:
    cleaned_platform = _coerce_enum(platform, AccessPlatform, "platform")
    cleaned_status = _coerce_enum(status, AccessStatus, "status")
    cleaned_notes = _optional_text(notes, "notes", NOTES_MAX)
    reject_secrets(cleaned_notes, "notes")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            existing = scope.select(
                "agy_access_items",
                where="platform = ?",
                params=(cleaned_platform.value,),
                limit=1,
            )
            if existing:
                raise AgencyConflict("That platform is already on the access checklist.")
            record_id = new_id("acc")
            now = _now()
            scope.insert(
                "agy_access_items",
                {
                    "id": record_id,
                    "platform": cleaned_platform.value,
                    "status": cleaned_status.value,
                    "notes": cleaned_notes,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_access_items", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_access(row)


def list_access(brand_id: str, *, limit: int = 100, offset: int = 0) -> list[AccessItem]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_access_items",
            order="platform ASC, id ASC",
            limit=limit_value,
            offset=offset_value,
        )
    return [_row_access(row) for row in rows]


def get_access(brand_id: str, item_id: str) -> AccessItem | None:
    with agency_conn() as conn:
        row = BrandScope(conn, brand_id).get("agy_access_items", item_id)
    return _row_access(row) if row else None


def update_access(brand_id: str, item_id: str, /, **fields: Any) -> AccessItem | None:
    if "brand_id" in fields:
        raise AgencyError("Moving a record to another brand is not possible.")
    if "platform" in fields:
        raise AgencyError("platform cannot be changed.")
    reject_secrets(fields, "access")
    unknown = set(fields) - {"status", "notes"}
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = scope.get("agy_access_items", item_id)
            if current is None:
                return None
            status = AccessStatus(current["status"])
            notes = current.get("notes") or ""
            if "status" in fields:
                status = _coerce_enum(fields["status"], AccessStatus, "status")
            if "notes" in fields:
                notes = _optional_text(fields["notes"], "notes", NOTES_MAX)
            reject_secrets(notes, "notes")
            scope.update(
                "agy_access_items",
                item_id,
                {"status": status.value, "notes": notes, "updated_at": _now()},
            )
            row = scope.get("agy_access_items", item_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_access(row)


def delete_access(brand_id: str, item_id: str) -> bool:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_access_items", item_id) is None:
                return False
            scope.delete("agy_access_items", item_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    return True


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------


def _filled(profile: dict[str, Any], name: str) -> bool:
    value = profile.get(name)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def onboarding_status(brand_id: str) -> dict[str, Any]:
    """Steps, a completion percentage, and the items still missing.

    A brand with no profile yet is an empty sheet. The percentage stays below
    100 until every profile field is filled and every checklist row is either
    granted or not needed. An empty checklist is itself a missing item.
    """
    profile_row = get_profile(brand_id)
    if profile_row is None:
        sheet = _empty_profile()
    else:
        sheet = {
            name: getattr(profile_row, name)
            for name in _PROFILE_FIELDS
        }
    access = list_access(brand_id, limit=200)

    steps: list[dict[str, Any]] = []
    missing: list[str] = []
    done = 0
    for step_id, title, fields in _STEPS:
        step_missing = [name for name in fields if not _filled(sheet, name)]
        done += len(fields) - len(step_missing)
        missing.extend(step_missing)
        steps.append(
            {
                "id": step_id,
                "title": title,
                "complete": not step_missing,
                "missing": step_missing,
            }
        )

    access_missing: list[str] = []
    if not access:
        access_missing.append("access checklist")
    else:
        for item in access:
            if item.status.value in _SETTLED:
                done += 1
            else:
                access_missing.append(f"{item.platform.value} access")
    missing.extend(access_missing)
    steps.append(
        {
            "id": "access",
            "title": "Platform access",
            "complete": not access_missing,
            "missing": access_missing,
        }
    )

    total = done + len(missing)
    percent = 0 if total == 0 else (done * 100) // total
    return {
        "brand_id": brand_id,
        "completion_percent": percent,
        "missing": missing,
        "steps": steps,
    }
