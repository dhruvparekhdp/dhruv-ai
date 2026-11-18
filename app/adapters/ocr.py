"""OCR service adapter.

POSTs one file to `/v1/extract` and returns fields. The call goes through
`AdapterClient`, so a timeout, a 5xx, or an open circuit is `unavailable`
and does not raise. POST is not retried.

When `OCR_BASE_URL` is unset the adapter is a stub: it does not open a
socket and it does not invent fields. A fabricated invoice number would be
worse than saying the service is down.

The service speaks the JAR-011 envelope. Fields live in `data.fields`, each
with name, value, confidence, page, and bbox.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.adapters.client import AdapterClient
from app.adapters.models import AdapterResponse, Outcome

EXTRACT_PATH = "/v1/extract"
ADAPTER_NAME = "ocr"
AUTH_ENV = "OCR_API_TOKEN"
# A scan is not a quote. Two minutes still marks a clock-skewed stamp as stale
# without treating a just-extracted document as old.
STALE_AFTER = timedelta(seconds=120)
DEFAULT_REVIEW_BELOW = Decimal("0.8")


@dataclass(frozen=True)
class OcrField:
    name: str
    value: str
    confidence: Decimal
    page: int
    bbox: tuple[Decimal, Decimal, Decimal, Decimal]
    needs_review: bool


@dataclass(frozen=True)
class OcrRead:
    outcome: Outcome
    reason: str
    detail: str
    fields: tuple[OcrField, ...]
    data_as_of: str | None
    stale: bool
    correlation_id: str
    review_below: Decimal
    http_status: int | None = None


class OcrAdapter:
    """One OCR service. `client` is None when the base URL is not configured."""

    def __init__(self, client: AdapterClient | None, *, review_below: Decimal) -> None:
        self.client = client
        self.review_below = review_below

    async def extract(self, filename: str, content: bytes, content_type: str) -> OcrRead:
        if self.client is None:
            return _empty(
                outcome=Outcome.UNAVAILABLE,
                reason="not_configured",
                detail="OCR_BASE_URL is not set",
                review_below=self.review_below,
            )
        safe_name = _basename(filename)
        response = await self.client.request(
            "POST",
            EXTRACT_PATH,
            files={"file": (safe_name, content, content_type or "application/octet-stream")},
        )
        return interpret(response, self.review_below)


def build_ocr_adapter(**overrides: Any) -> OcrAdapter:
    """Build from the environment, unless the caller passes a base URL.

    An empty base URL returns the stub. Tests pass `base_url` and `secrets`
    so they never read a real token.
    """
    review_below = overrides.pop("review_below", review_threshold())
    base_url = overrides.pop("base_url", os.getenv("OCR_BASE_URL", "")).strip()
    if not base_url:
        return OcrAdapter(None, review_below=review_below)
    client = AdapterClient(
        name=overrides.pop("name", ADAPTER_NAME),
        base_url=base_url,
        stale_after=overrides.pop("stale_after", STALE_AFTER),
        auth_env=overrides.pop("auth_env", AUTH_ENV),
        schema=overrides.pop("schema", validate_ocr_envelope),
        connect_timeout=overrides.pop("connect_timeout", 3.0),
        read_timeout=overrides.pop("read_timeout", 30.0),
        max_retries=overrides.pop("max_retries", 2),
        **overrides,
    )
    return OcrAdapter(client, review_below=review_below)


def review_threshold() -> Decimal:
    """`OCR_REVIEW_CONFIDENCE`. A bad value keeps 0.8. The value is not a secret."""
    raw = os.getenv("OCR_REVIEW_CONFIDENCE", "").strip()
    if not raw:
        return DEFAULT_REVIEW_BELOW
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return DEFAULT_REVIEW_BELOW
    if value < 0 or value > 1:
        return DEFAULT_REVIEW_BELOW
    return value


def validate_ocr_envelope(body: Mapping[str, Any]) -> str | None:
    """Schema hook. The envelope itself was already checked."""
    data = body.get("data")
    if not isinstance(data, dict):
        return "data must be an object"
    fields = data.get("fields")
    if not isinstance(fields, list):
        return "data.fields must be an array"
    for index, field in enumerate(fields):
        problem = _field_problem(index, field)
        if problem:
            return problem
    return None


def interpret(response: AdapterResponse, review_below: Decimal) -> OcrRead:
    if response.outcome not in {Outcome.OK, Outcome.STALE}:
        return _empty(
            outcome=response.outcome,
            reason=response.reason,
            detail=response.detail,
            review_below=review_below,
            data_as_of=response.data_as_of,
            stale=response.stale,
            correlation_id=response.correlation_id,
            http_status=response.http_status,
        )
    data = response.data if isinstance(response.data, dict) else {}
    raw_fields = data.get("fields") if isinstance(data.get("fields"), list) else []
    fields = tuple(_to_field(item, review_below) for item in raw_fields)
    return OcrRead(
        outcome=response.outcome,
        reason=response.reason,
        detail=response.detail,
        fields=fields,
        data_as_of=response.data_as_of,
        stale=response.stale,
        correlation_id=response.correlation_id,
        review_below=review_below,
        http_status=response.http_status,
    )


def render_read(read: OcrRead) -> str:
    """Text an agent can quote. Unavailable and invalid reads have no fields."""
    if read.outcome is Outcome.UNAVAILABLE:
        return "UNAVAILABLE\n" + (read.detail or read.reason or "ocr service unavailable")
    if read.outcome is Outcome.INVALID:
        return "INVALID\n" + (read.detail or read.reason or "ocr response was invalid")
    lines: list[str] = []
    if read.stale or read.outcome is Outcome.STALE:
        lines.append("STALE")
    lines.append("SOURCE ocr")
    lines.append(f"DATA_AS_OF {read.data_as_of or 'unknown'}")
    lines.append(f"REVIEW_BELOW {format(read.review_below, 'f')}")
    if not read.fields:
        lines.append("no fields")
        return "\n".join(lines)
    for field in read.fields:
        lines.append("")
        lines.append(f"{field.name}: {field.value}")
        lines.append(f"confidence: {format(field.confidence, 'f')}")
        lines.append(f"page: {field.page}")
        lines.append("bbox: " + ",".join(format(number, "f") for number in field.bbox))
        if field.needs_review:
            lines.append("review: low confidence")
    return "\n".join(lines)


def _to_field(item: Any, review_below: Decimal) -> OcrField:
    confidence = _decimal(item["confidence"])
    bbox = item["bbox"]
    return OcrField(
        name=str(item["name"]),
        value=str(item["value"]),
        confidence=confidence,
        page=int(item["page"]),
        bbox=(_decimal(bbox[0]), _decimal(bbox[1]), _decimal(bbox[2]), _decimal(bbox[3])),
        needs_review=confidence < review_below,
    )


def _field_problem(index: int, field: Any) -> str | None:
    where = f"fields[{index}]"
    if not isinstance(field, dict):
        return f"{where} must be an object"
    for key in ("name", "value", "confidence", "page", "bbox"):
        if key not in field:
            return f"{where} missing {key}"
    if not isinstance(field["name"], str) or not field["name"].strip():
        return f"{where} name must be a non-empty string"
    if not isinstance(field["value"], str):
        return f"{where} value must be a string"
    confidence = _decimal_or_none(field["confidence"])
    if confidence is None or confidence < 0 or confidence > 1:
        return f"{where} confidence must be between 0 and 1"
    page = field["page"]
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return f"{where} page must be an integer >= 1"
    bbox = field["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        return f"{where} bbox must be four numbers"
    if any(_decimal_or_none(number) is None for number in bbox):
        return f"{where} bbox must be four numbers"
    return None


def _empty(
    *,
    outcome: Outcome,
    reason: str,
    detail: str,
    review_below: Decimal,
    data_as_of: str | None = None,
    stale: bool = False,
    correlation_id: str = "",
    http_status: int | None = None,
) -> OcrRead:
    return OcrRead(
        outcome=outcome,
        reason=reason,
        detail=detail,
        fields=(),
        data_as_of=data_as_of,
        stale=stale,
        correlation_id=correlation_id,
        review_below=review_below,
        http_status=http_status,
    )


def _decimal(value: Any) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None:
        raise ValueError("not a decimal")
    return parsed


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _basename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        return "document"
    return name
