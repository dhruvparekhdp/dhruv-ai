"""Result types agents are allowed to see.

A failed upstream is a value (`unavailable`), not an exception. An exception
crossing into an agent becomes either a crashed task or a fabricated recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


#: Reasons that mean "do not treat this payload as fresh truth".
UNAVAILABLE_REASONS = frozenset(
    {
        "timeout",
        "http_5xx",
        "circuit_open",
        "network",
        "missing_credential",
        "http_4xx",
        "http_3xx",
        "internal",
    }
)


@dataclass(frozen=True)
class AdapterResponse:
    """One call's outcome. `data` is present for ok and stale, empty otherwise."""

    adapter: str
    outcome: Outcome
    correlation_id: str
    data: Any = None
    data_as_of: str | None = None
    stale: bool = False
    reason: str = ""
    detail: str = ""
    http_status: int | None = None
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "outcome": self.outcome.value,
            "correlation_id": self.correlation_id,
            "data": self.data,
            "data_as_of": self.data_as_of,
            "stale": self.stale,
            "reason": self.reason,
            "detail": self.detail,
            "http_status": self.http_status,
            "checked_at": self.checked_at,
        }
