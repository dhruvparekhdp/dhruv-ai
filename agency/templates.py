"""Seeded channel templates and the character limits on their fields.

The catalog is shared. Validating a draft reads it and returns errors. It does
not store the draft, publish it, or send it to an ad network. The submitted
text is not copied into the error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agency.db import agency_conn
from agency.models import AgencyError, ChannelPlatform, ChannelTemplate, TemplateField
from agency.store import _clean_id, _coerce_enum
from app.core.db import fetch_all, fetch_one

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FIELD_CAP = 30


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


@dataclass(frozen=True)
class TemplateCheck:
    template_id: str
    valid: bool
    errors: tuple[FieldError, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "valid": self.valid,
            "errors": [item.to_dict() for item in self.errors],
        }


def _row_template(row: dict[str, Any]) -> ChannelTemplate:
    return ChannelTemplate(
        id=row["id"],
        platform=ChannelPlatform(row["platform"]),
        slug=row["slug"],
        name=row["name"],
        sort_order=int(row["sort_order"]),
    )


def _row_field(row: dict[str, Any]) -> TemplateField:
    return TemplateField(
        id=row["id"],
        template_id=row["template_id"],
        field_key=row["field_key"],
        label=row["label"],
        max_chars=int(row["max_chars"]),
        min_count=int(row["min_count"]),
        max_count=int(row["max_count"]),
        sort_order=int(row["sort_order"]),
    )


def _load(conn, template_id: str) -> tuple[ChannelTemplate, list[TemplateField]] | None:
    row = fetch_one(conn, "SELECT * FROM agy_channel_templates WHERE id = ?", (template_id,))
    if row is None:
        return None
    fields = fetch_all(
        conn,
        "SELECT * FROM agy_template_fields WHERE template_id = ?"
        " ORDER BY sort_order ASC, field_key ASC",
        (template_id,),
    )
    return _row_template(row), [_row_field(item) for item in fields]


def list_templates(platform: str | None = None) -> list[dict[str, Any]]:
    """Every seeded template, optionally one platform, fields included."""
    chosen = ""
    if platform:
        chosen = _coerce_enum(platform, ChannelPlatform, "platform").value
    with agency_conn() as conn:
        if chosen:
            rows = fetch_all(
                conn,
                "SELECT * FROM agy_channel_templates WHERE platform = ? ORDER BY sort_order ASC, id ASC",
                (chosen,),
            )
        else:
            rows = fetch_all(
                conn,
                "SELECT * FROM agy_channel_templates ORDER BY sort_order ASC, id ASC",
            )
        listed: list[dict[str, Any]] = []
        for row in rows:
            loaded = _load(conn, row["id"])
            assert loaded is not None
            template, fields = loaded
            payload = template.to_dict()
            payload["fields"] = [field.to_dict() for field in fields]
            listed.append(payload)
    return listed


def get_template(template_id: str) -> dict[str, Any] | None:
    cleaned = _clean_id(template_id, "template_id", required=True)
    assert cleaned is not None
    with agency_conn() as conn:
        loaded = _load(conn, cleaned)
    if loaded is None:
        return None
    template, fields = loaded
    payload = template.to_dict()
    payload["fields"] = [field.to_dict() for field in fields]
    return payload


def _texts(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = value
    else:
        raise AgencyError(f"{field} must be text.")
    texts: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AgencyError(f"{field} must be text.")
        stripped = item.strip()
        if stripped:
            texts.append(stripped)
    return texts


def validate_template(template_id: str, fields: dict[str, Any] | None) -> TemplateCheck | None:
    """Check ``fields`` against the template. ``None`` when the template is absent.

    A string is one value. A list is several. Blank values are ignored. Length
    is the number of characters after stripping the ends, and the message
    reports the length and the limit without the text itself.
    """
    cleaned = _clean_id(template_id, "template_id", required=True)
    assert cleaned is not None
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise AgencyError("fields must be an object.")
    if len(fields) > _FIELD_CAP:
        raise AgencyError("Too many fields.")
    with agency_conn() as conn:
        loaded = _load(conn, cleaned)
    if loaded is None:
        return None
    _template, defined = loaded
    by_key = {field.field_key: field for field in defined}
    errors: list[FieldError] = []
    for key in fields:
        if key not in by_key:
            label = key if _KEY.fullmatch(str(key)) else ""
            suffix = f" '{label}'" if label else ""
            errors.append(FieldError(str(key) if label else "", f"Unknown field{suffix}."))
    for field in defined:
        if field.field_key not in fields:
            texts: list[str] = []
        else:
            texts = _texts(fields[field.field_key], field.field_key)
        if len(texts) > field.max_count:
            errors.append(
                FieldError(
                    field.field_key,
                    f"{field.label} allows at most {field.max_count} values.",
                )
            )
        elif len(texts) < field.min_count:
            errors.append(
                FieldError(
                    field.field_key,
                    f"{field.label} needs at least {field.min_count} values.",
                )
            )
        for index, text in enumerate(texts[: field.max_count], start=1):
            if len(text) <= field.max_chars:
                continue
            where = f"{field.label} {index}" if field.max_count > 1 else field.label
            errors.append(
                FieldError(
                    field.field_key,
                    f"{where} is {len(text)} characters. The limit is {field.max_chars}.",
                )
            )
    return TemplateCheck(cleaned, not errors, tuple(errors))
