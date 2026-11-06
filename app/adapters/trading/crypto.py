"""crypto-signal-engine adapter. Read-only. Paper, shadow, or research."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.adapters.trading.base import TradingAdapter, build_feed
from app.contracts.trading_v1.models import SourceEngine

AUTH_ENV = "CRYPTO_INTEGRATION_API_TOKEN"
# Quotes move quickly. Older than two minutes is stale even if the engine
# still labels the row fresh.
STALE_AFTER = timedelta(seconds=120)


def build_crypto_adapter(base_url: str, **client_kwargs: Any) -> TradingAdapter:
    return build_feed(
        name="crypto-signal-engine",
        base_url=base_url,
        source=SourceEngine.CRYPTO,
        auth_env=AUTH_ENV,
        stale_after=client_kwargs.pop("stale_after", STALE_AFTER),
        **client_kwargs,
    )
