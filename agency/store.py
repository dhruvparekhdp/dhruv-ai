"""Durable brand, client and contact records.

The other agency entities exist in the schema so later work has somewhere to
go. This module only writes the three roots a person has to create before any
of that work makes sense. Rules live here, not in the router: a check that
only the HTTP layer knows about is a suggestion.

Clients belong to a personal or agency brand. A brand of kind ``client`` is a
workspace, not an owner of other clients. A contact's brand is either the
client's owning brand or the linked client brand — never an unrelated one.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from agency.db import agency_conn
from agency.models import (
    EMAIL_MAX,
    ID_PATTERN,
    NAME_MAX,
    NOTES_MAX,
    PHONE_MAX,
    ROLE_MAX,
    SLUG_MAX,
    SLUG_PATTERN,
    URL_MAX,
    AgencyConflict,
    AgencyError,
    Brand,
    BrandKind,
    Client,
    ClientStatus,
    Contact,
    new_id,
)
from agency.scope import BrandScope, constraint_rows, lookup
from app.core.db import execute, fetch_all, fetch_one

_E = TypeVar("_E", bound=Enum)
_SLUG_RE = re.compile(SLUG_PATTERN)
_ID_RE = re.compile(ID_PATTERN)
_OWNER_KINDS = {BrandKind.PERSONAL.value, BrandKind.AGENCY.value}

try:
    import psycopg
except ImportError:  # sqlite-only installs still load this module
    _INTEGRITY: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)
else:
    _INTEGRITY = (sqlite3.IntegrityError, psycopg.IntegrityError)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: Any) -> datetime | None:
    """Normalise a stored timestamp to an aware datetime.

    Columns are TEXT, so both backends hand back the ISO string we wrote.
    The parser still accepts a native datetime in case a future column type
    changes that — comparing the two shapes as strings is what D-005 forbids.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _raise_integrity(exc: BaseException) -> None:
    text = str(exc).lower()
    if "foreign key" in text:
        raise AgencyConflict("That record is still referenced and cannot be changed.") from exc
    if "unique" in text or "duplicate" in text:
        raise AgencyConflict("A record with that unique value already exists.") from exc
    if "not null" in text or "null value" in text or "check constraint" in text:
        raise AgencyError("The record does not satisfy the agency schema.") from exc
    raise AgencyConflict("The change conflicts with existing data.") from exc


def _bound(value: Any, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AgencyError(f"{field} must be an integer.") from exc
    if not low <= number <= high:
        raise AgencyError(f"{field} must be between {low} and {high}.")
    return number


def _require_text(value: Any, field: str, max_len: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgencyError(f"{field} is required.")
    if len(text) > max_len:
        raise AgencyError(f"{field} must be at most {max_len} characters.")
    return text


def _optional_text(value: Any, field: str, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        raise AgencyError(f"{field} must be at most {max_len} characters.")
    return text


def _clean_id(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise AgencyError(f"{field} is required.")
        return None
    text = str(value).strip()
    if _ID_RE.fullmatch(text) is None:
        raise AgencyError(f"{field} is not a valid id.")
    return text


def _coerce_enum(value: Any, enum_cls: type[_E], field: str) -> _E:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(item.value for item in enum_cls)
        raise AgencyError(f"Unknown {field} '{value}'. Known: {known}") from exc


def _clean_slug(value: str) -> str:
    slug = value.strip().lower()
    if _SLUG_RE.fullmatch(slug) is None or len(slug) > SLUG_MAX:
        raise AgencyError(
            "slug must be lowercase words separated by hyphens, at most "
            f"{SLUG_MAX} characters."
        )
    return slug


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = slug[:SLUG_MAX].strip("-")
    return slug or "brand"


def _clean_email(value: Any) -> str:
    email = _optional_text(value, "email", EMAIL_MAX).lower()
    if not email:
        return ""
    if email.count("@") != 1 or any(character.isspace() for character in email):
        raise AgencyError("email is not a valid address.")
    local, domain = email.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise AgencyError("email is not a valid address.")
    return email


def _slug_taken(conn, slug: str, exclude_id: str | None) -> bool:
    row = fetch_one(conn, "SELECT id FROM agy_brands WHERE slug = ?", (slug,))
    return row is not None and row["id"] != exclude_id


def _allocate_slug(conn, base: str, exclude_id: str | None) -> str:
    candidate = base
    for number in range(2, 102):
        if not _slug_taken(conn, candidate, exclude_id):
            return candidate
        suffix = f"-{number}"
        trimmed = base[: SLUG_MAX - len(suffix)].strip("-") or "brand"
        candidate = f"{trimmed}{suffix}"
    raise AgencyError("Could not allocate a unique slug.")


def _row_brand(row: dict[str, Any]) -> Brand:
    return Brand(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        kind=BrandKind(row["kind"]),
        notes=row.get("notes") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _row_client(row: dict[str, Any]) -> Client:
    return Client(
        id=row["id"],
        brand_id=row["brand_id"],
        name=row["name"],
        status=ClientStatus(row["status"]),
        client_brand_id=row.get("client_brand_id"),
        website=row.get("website") or "",
        notes=row.get("notes") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _row_contact(row: dict[str, Any]) -> Contact:
    return Contact(
        id=row["id"],
        brand_id=row["brand_id"],
        name=row["name"],
        client_id=row.get("client_id"),
        email=row.get("email") or "",
        phone=row.get("phone") or "",
        role=row.get("role") or "",
        notes=row.get("notes") or "",
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


def _brand_row(conn, brand_id: str) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT id, kind FROM agy_brands WHERE id = ?", (brand_id,))
    if row is None:
        raise AgencyError(f"Unknown brand_id '{brand_id}'.")
    return row


def _assert_owner_brand(conn, brand_id: str) -> None:
    row = _brand_row(conn, brand_id)
    if row["kind"] not in _OWNER_KINDS:
        raise AgencyError("Clients belong to a personal or agency brand, not a client brand.")


def _assert_client_brand(conn, client_brand_id: str | None, owner_brand_id: str) -> None:
    if client_brand_id is None:
        return
    if client_brand_id == owner_brand_id:
        raise AgencyError("client_brand_id must be a different brand from the owning brand.")
    row = _brand_row(conn, client_brand_id)
    if row["kind"] != BrandKind.CLIENT.value:
        raise AgencyError("client_brand_id must reference a brand of kind 'client'.")


def _assert_contacts_still_fit(
    conn, client_id: str, owner_brand_id: str, client_brand_id: str | None
) -> None:
    """Moving a client must not leave a contact scoped to a brand it can no longer see."""
    allowed = {owner_brand_id}
    if client_brand_id:
        allowed.add(client_brand_id)
    rows = constraint_rows(conn, "agy_contacts", "client_id = ?", (client_id,))
    if any(row["brand_id"] not in allowed for row in rows):
        raise AgencyError(
            "Cannot change brand linkage while a contact is scoped to a brand outside it."
        )


def _assert_contact_link(conn, brand_id: str, client_id: str | None) -> None:
    _brand_row(conn, brand_id)
    if client_id is None:
        return
    client = lookup(conn, "agy_clients", client_id)
    if client is None:
        raise AgencyError(f"Unknown client_id '{client_id}'.")
    allowed = {client["brand_id"]}
    if client["client_brand_id"]:
        allowed.add(client["client_brand_id"])
    if brand_id not in allowed:
        raise AgencyError(
            "Contact brand must be the client's owning brand or its client brand."
        )


# --------------------------------------------------------------------------
# Brands
# --------------------------------------------------------------------------


def create_brand(
    name: str,
    kind: str | BrandKind,
    *,
    slug: str | None = None,
    notes: str = "",
    brand_id: str | None = None,
) -> Brand:
    try:
        with agency_conn() as conn:
            cleaned_name = _require_text(name, "name", NAME_MAX)
            cleaned_kind = _coerce_enum(kind, BrandKind, "kind")
            cleaned_notes = _optional_text(notes, "notes", NOTES_MAX)
            record_id = _clean_id(brand_id, "id", required=True) if brand_id else new_id("brd")
            if slug:
                cleaned_slug = _clean_slug(slug)
                if _slug_taken(conn, cleaned_slug, None):
                    raise AgencyConflict(f"Slug '{cleaned_slug}' is already in use.")
            else:
                cleaned_slug = _allocate_slug(conn, slugify(cleaned_name), None)
            now = _now()
            execute(
                conn,
                "INSERT INTO agy_brands (id, name, slug, kind, notes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    cleaned_name,
                    cleaned_slug,
                    cleaned_kind.value,
                    cleaned_notes,
                    now,
                    now,
                ),
            )
            row = fetch_one(conn, "SELECT * FROM agy_brands WHERE id = ?", (record_id,))
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_brand(row)


def get_brand(brand_id: str) -> Brand | None:
    with agency_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM agy_brands WHERE id = ?", (brand_id,))
    return _row_brand(row) if row else None


def list_brands(
    *,
    kind: str | BrandKind | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Brand]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    params: list[Any] = []
    where = ""
    if kind:
        cleaned = _coerce_enum(kind, BrandKind, "kind")
        where = " WHERE kind = ?"
        params.append(cleaned.value)
    params.extend([limit_value, offset_value])
    with agency_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM agy_brands" + where + " ORDER BY name ASC, id ASC LIMIT ? OFFSET ?",
            params,
        )
    return [_row_brand(row) for row in rows]


def update_brand(brand_id: str, **fields: Any) -> Brand | None:
    unknown = set(fields) - {"name", "kind", "slug", "notes"}
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    try:
        with agency_conn() as conn:
            current = fetch_one(conn, "SELECT * FROM agy_brands WHERE id = ?", (brand_id,))
            if current is None:
                return None
            if not fields:
                return _row_brand(current)
            name = current["name"]
            kind = BrandKind(current["kind"])
            slug = current["slug"]
            notes = current["notes"] or ""
            if "name" in fields:
                name = _require_text(fields["name"], "name", NAME_MAX)
            if "notes" in fields:
                notes = _optional_text(fields["notes"], "notes", NOTES_MAX)
            if "slug" in fields:
                slug = _clean_slug(str(fields["slug"]))
                if _slug_taken(conn, slug, brand_id):
                    raise AgencyConflict(f"Slug '{slug}' is already in use.")
            if "kind" in fields:
                kind = _coerce_enum(fields["kind"], BrandKind, "kind")
                _assert_kind_change(conn, brand_id, kind)
            now = _now()
            execute(
                conn,
                "UPDATE agy_brands SET name = ?, slug = ?, kind = ?, notes = ?, updated_at = ?"
                " WHERE id = ?",
                (name, slug, kind.value, notes, now, brand_id),
            )
            row = fetch_one(conn, "SELECT * FROM agy_brands WHERE id = ?", (brand_id,))
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_brand(row)


def _assert_kind_change(conn, brand_id: str, new_kind: BrandKind) -> None:
    if new_kind is BrandKind.CLIENT:
        rows = BrandScope(conn, brand_id).select("agy_clients", limit=1)
        if rows:
            raise AgencyError("This brand owns clients, so it cannot become a client brand.")
        return
    rows = constraint_rows(conn, "agy_clients", "client_brand_id = ?", (brand_id,))
    if rows:
        raise AgencyError("This brand is linked as a client brand. Kind must stay 'client'.")


def delete_brand(brand_id: str) -> bool:
    try:
        with agency_conn() as conn:
            if fetch_one(conn, "SELECT id FROM agy_brands WHERE id = ?", (brand_id,)) is None:
                return False
            execute(conn, "DELETE FROM agy_brands WHERE id = ?", (brand_id,))
    except _INTEGRITY as exc:
        raise AgencyConflict(
            "Brand is still referenced. Remove the records that point at it first."
        ) from exc
    return True


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


def create_client(
    brand_id: str,
    name: str,
    *,
    status: str | ClientStatus = ClientStatus.ACTIVE,
    client_brand_id: str | None = None,
    website: str = "",
    notes: str = "",
    client_id: str | None = None,
) -> Client:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            record_id = _clean_id(client_id, "id", required=True) if client_id else new_id("cli")
            assert record_id is not None
            linked = _clean_id(client_brand_id, "client_brand_id")
            _assert_owner_brand(conn, scope.brand_id)
            _assert_client_brand(conn, linked, scope.brand_id)
            now = _now()
            scope.insert(
                "agy_clients",
                {
                    "id": record_id,
                    "client_brand_id": linked,
                    "name": _require_text(name, "name", NAME_MAX),
                    "status": _coerce_enum(status, ClientStatus, "status").value,
                    "website": _optional_text(website, "website", URL_MAX),
                    "notes": _optional_text(notes, "notes", NOTES_MAX),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_clients", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_client(row)


def get_client(brand_id: str, client_id: str) -> Client | None:
    with agency_conn() as conn:
        row = BrandScope(conn, brand_id).get("agy_clients", client_id)
    return _row_client(row) if row else None


def list_clients(
    brand_id: str,
    *,
    status: str | ClientStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Client]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    where = ""
    params: list[Any] = []
    if status:
        where = "status = ?"
        params.append(_coerce_enum(status, ClientStatus, "status").value)
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_clients",
            where=where,
            params=params,
            order="updated_at DESC, id ASC",
            limit=limit_value,
            offset=offset_value,
        )
    return [_row_client(row) for row in rows]


def update_client(brand_id: str, client_id: str, /, **fields: Any) -> Client | None:
    unknown = set(fields) - {"brand_id", "name", "status", "client_brand_id", "website", "notes"}
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = scope.get("agy_clients", client_id)
            if current is None:
                return None
            if not fields:
                return _row_client(current)
            owner = current["brand_id"]
            name = current["name"]
            status = ClientStatus(current["status"])
            linked = current.get("client_brand_id")
            website = current.get("website") or ""
            notes = current.get("notes") or ""
            if "name" in fields:
                name = _require_text(fields["name"], "name", NAME_MAX)
            if "status" in fields:
                status = _coerce_enum(fields["status"], ClientStatus, "status")
            if "website" in fields:
                website = _optional_text(fields["website"], "website", URL_MAX)
            if "notes" in fields:
                notes = _optional_text(fields["notes"], "notes", NOTES_MAX)
            if "brand_id" in fields:
                cleaned = _clean_id(fields["brand_id"], "brand_id", required=True)
                assert cleaned is not None
                owner = cleaned
            if "client_brand_id" in fields:
                linked = _clean_id(fields["client_brand_id"], "client_brand_id")
            _assert_owner_brand(conn, owner)
            _assert_client_brand(conn, linked, owner)
            if "brand_id" in fields or "client_brand_id" in fields:
                _assert_contacts_still_fit(conn, client_id, owner, linked)
            now = _now()
            changed = scope.update(
                "agy_clients",
                client_id,
                {
                    "brand_id": owner,
                    "client_brand_id": linked,
                    "name": name,
                    "status": status.value,
                    "website": website,
                    "notes": notes,
                    "updated_at": now,
                },
                allow_brand_change=True,
            )
            if changed == 0:
                return None
            row = BrandScope(conn, owner).get("agy_clients", client_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_client(row)


def delete_client(brand_id: str, client_id: str) -> bool:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_clients", client_id) is None:
                return False
            scope.delete("agy_clients", client_id)
    except _INTEGRITY as exc:
        raise AgencyConflict(
            "Client is still referenced. Remove the records that point at it first."
        ) from exc
    return True


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------


def create_contact(
    brand_id: str,
    name: str,
    *,
    client_id: str | None = None,
    email: str = "",
    phone: str = "",
    role: str = "",
    notes: str = "",
    contact_id: str | None = None,
) -> Contact:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            linked = _clean_id(client_id, "client_id")
            _assert_contact_link(conn, scope.brand_id, linked)
            record_id = _clean_id(contact_id, "id", required=True) if contact_id else new_id("cnt")
            assert record_id is not None
            now = _now()
            scope.insert(
                "agy_contacts",
                {
                    "id": record_id,
                    "client_id": linked,
                    "name": _require_text(name, "name", NAME_MAX),
                    "email": _clean_email(email),
                    "phone": _optional_text(phone, "phone", PHONE_MAX),
                    "role": _optional_text(role, "role", ROLE_MAX),
                    "notes": _optional_text(notes, "notes", NOTES_MAX),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = scope.get("agy_contacts", record_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_contact(row)


def get_contact(brand_id: str, contact_id: str) -> Contact | None:
    with agency_conn() as conn:
        row = BrandScope(conn, brand_id).get("agy_contacts", contact_id)
    return _row_contact(row) if row else None


def list_contacts(
    brand_id: str,
    *,
    client_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Contact]:
    limit_value = _bound(limit, "limit", 1, 200)
    offset_value = _bound(offset, "offset", 0, 100_000)
    where = ""
    params: list[Any] = []
    if client_id:
        where = "client_id = ?"
        params.append(_clean_id(client_id, "client_id", required=True))
    with agency_conn() as conn:
        rows = BrandScope(conn, brand_id).select(
            "agy_contacts",
            where=where,
            params=params,
            order="name ASC, id ASC",
            limit=limit_value,
            offset=offset_value,
        )
    return [_row_contact(row) for row in rows]


def update_contact(brand_id: str, contact_id: str, /, **fields: Any) -> Contact | None:
    unknown = set(fields) - {"brand_id", "name", "client_id", "email", "phone", "role", "notes"}
    if unknown:
        raise AgencyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            current = scope.get("agy_contacts", contact_id)
            if current is None:
                return None
            if not fields:
                return _row_contact(current)
            owner = current["brand_id"]
            name = current["name"]
            linked = current.get("client_id")
            email = current.get("email") or ""
            phone = current.get("phone") or ""
            role = current.get("role") or ""
            notes = current.get("notes") or ""
            if "name" in fields:
                name = _require_text(fields["name"], "name", NAME_MAX)
            if "email" in fields:
                email = _clean_email(fields["email"])
            if "phone" in fields:
                phone = _optional_text(fields["phone"], "phone", PHONE_MAX)
            if "role" in fields:
                role = _optional_text(fields["role"], "role", ROLE_MAX)
            if "notes" in fields:
                notes = _optional_text(fields["notes"], "notes", NOTES_MAX)
            if "brand_id" in fields:
                cleaned = _clean_id(fields["brand_id"], "brand_id", required=True)
                assert cleaned is not None
                owner = cleaned
            if "client_id" in fields:
                linked = _clean_id(fields["client_id"], "client_id")
            _assert_contact_link(conn, owner, linked)
            now = _now()
            changed = scope.update(
                "agy_contacts",
                contact_id,
                {
                    "brand_id": owner,
                    "client_id": linked,
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "role": role,
                    "notes": notes,
                    "updated_at": now,
                },
                allow_brand_change=True,
            )
            if changed == 0:
                return None
            row = BrandScope(conn, owner).get("agy_contacts", contact_id)
    except _INTEGRITY as exc:
        _raise_integrity(exc)
    assert row is not None
    return _row_contact(row)


def delete_contact(brand_id: str, contact_id: str) -> bool:
    try:
        with agency_conn() as conn:
            scope = BrandScope(conn, brand_id)
            if scope.get("agy_contacts", contact_id) is None:
                return False
            scope.delete("agy_contacts", contact_id)
    except _INTEGRITY as exc:
        raise AgencyConflict(
            "Contact is still referenced. Remove the records that point at it first."
        ) from exc
    return True
