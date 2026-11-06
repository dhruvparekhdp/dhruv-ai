"""Response envelope: the only shape an adapter will treat as data.

Required:
  data         object or array
  data_as_of   ISO-8601 timestamp

Optional:
  stale        boolean, the engine's own opinion

Age is decided by the client, not by this module. An engine that sends
`stale: false` on an old `data_as_of` is still old.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

SchemaCheck = Callable[[Mapping[str, Any]], str | None]


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp as UTC. None when it is not one.

    Naive values are treated as UTC. Engines omit the zone often enough that
    rejecting them would mark healthy feeds invalid.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_envelope(payload: Any) -> str | None:
    """Return a short reason when `payload` is not an envelope, else None."""
    if not isinstance(payload, dict):
        return "envelope must be a JSON object"
    if "data" not in payload:
        return "missing data"
    if not isinstance(payload["data"], (dict, list)):
        return "data must be an object or array"
    if "data_as_of" not in payload:
        return "missing data_as_of"
    if parse_timestamp(payload["data_as_of"]) is None:
        return "data_as_of is not an ISO-8601 timestamp"
    if "stale" in payload and not isinstance(payload["stale"], bool):
        return "stale must be a boolean"
    return None


def require_data_fields(*fields: str) -> SchemaCheck:
    """Extra schema: `data` must be an object containing each named field."""

    def check(body: Mapping[str, Any]) -> str | None:
        data = body.get("data")
        if not isinstance(data, dict):
            return "data must be an object"
        missing = [name for name in fields if name not in data]
        if missing:
            return "data missing " + ", ".join(missing)
        return None

    return check
