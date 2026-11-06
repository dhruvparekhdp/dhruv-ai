"""NSE-TRADE adapter. Read-only. Indian equity research, paper, or shadow.

MIS and CNC are reporting labels on a position. They are not order instructions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.adapters.trading.base import TradingAdapter, build_feed
from app.contracts.trading_v1.models import SourceEngine

AUTH_ENV = "NSE_INTEGRATION_API_TOKEN"
# Cash-session marks can sit for a few minutes between polls.
STALE_AFTER = timedelta(seconds=180)


def build_nse_adapter(base_url: str, **client_kwargs: Any) -> TradingAdapter:
    return build_feed(
        name="nse-trade",
        base_url=base_url,
        source=SourceEngine.NSE,
        auth_env=AUTH_ENV,
        stale_after=client_kwargs.pop("stale_after", STALE_AFTER),
        **client_kwargs,
    )
