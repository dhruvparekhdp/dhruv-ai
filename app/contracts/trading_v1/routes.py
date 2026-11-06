"""Route table for trading-v1.

Only GET is defined. Any other method is 405. Nothing in this table mutates
an order, a transfer, or a strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALLOWED_METHOD = "GET"
BASE_PATH = "/integration/v1"

# Paths are fixed here. Response models live in models.py and are looked up
# by path so this module does not import them at load time.
PATHS: tuple[str, ...] = (
    "/integration/v1/health",
    "/integration/v1/market/overview",
    "/integration/v1/signals",
    "/integration/v1/positions",
    "/integration/v1/trades",
    "/integration/v1/performance",
    "/integration/v1/risk",
    "/integration/v1/strategies",
)

LIST_PATHS: frozenset[str] = frozenset(
    {
        "/integration/v1/signals",
        "/integration/v1/positions",
        "/integration/v1/trades",
        "/integration/v1/strategies",
    }
)

MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class RouteDecision:
    status_code: int
    path: str | None = None


def decide(method: str, path: str) -> RouteDecision:
    """405 for every non-GET. 404 for an unknown GET. 200 only for a known GET."""
    if method.upper() != ALLOWED_METHOD:
        return RouteDecision(405, path if path in PATHS else None)
    if path not in PATHS:
        return RouteDecision(404, None)
    return RouteDecision(200, path)


def is_list_path(path: str) -> bool:
    return path in LIST_PATHS


def response_model_for(path: str) -> Any:
    """Concrete envelope model for a known path. Import is local to keep routes light."""
    from app.contracts.trading_v1.models import RESPONSE_MODELS

    return RESPONSE_MODELS[path]
