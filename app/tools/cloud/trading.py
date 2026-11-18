"""Read-only tools over the crypto and NSE adapters.

Risk is READ: capability READ, risk SAFE. The names are not order-like, and
the permission check denies order-like names even if a definition lists one.
Nothing here places, cancels, amends, withdraws, or transfers.

The base URL comes from the environment at call time. When it is unset the
tool says so. Tests inject an adapter so the suite never dials a real engine.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from app.adapters.models import Outcome
from app.adapters.trading.base import TradingRead
from app.adapters.trading.crypto import build_crypto_adapter
from app.adapters.trading.nse import build_nse_adapter
from app.tools.registry import Capability, Risk, ToolError, tool

_LIST_RESOURCES = frozenset({"signals", "positions", "trades", "strategies"})
_RESOURCES = _LIST_RESOURCES | frozenset(
    {"health", "market_overview", "performance", "risk"}
)

# Short key -> (source_engine value, env var for the base URL, factory).
_ENGINES: dict[str, tuple[str, str, Any]] = {
    "crypto": ("crypto-signal-engine", "CRYPTO_ENGINE_BASE_URL", build_crypto_adapter),
    "nse": ("nse-trade", "NSE_ENGINE_BASE_URL", build_nse_adapter),
}

_injected: dict[str, Any] = {}


def set_trading_adapter(engine: str, adapter: Any | None) -> None:
    """Install or clear a test double. Production code leaves this empty."""
    if engine not in _ENGINES:
        raise ValueError(f"unknown engine {engine}")
    if adapter is None:
        _injected.pop(engine, None)
    else:
        _injected[engine] = adapter


def _resolve(engine: str) -> Any | None:
    if engine in _injected:
        return _injected[engine]
    _source, env_name, factory = _ENGINES[engine]
    base_url = os.environ.get(env_name, "").strip()
    if not base_url:
        return None
    return factory(base_url)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_read(*, source_engine: str, resource: str, read: TradingRead) -> str:
    """Header the summary validator parses, then the raw envelope for the model."""
    unavailable = read.outcome in {Outcome.UNAVAILABLE, Outcome.INVALID} or read.envelope is None
    if unavailable or read.envelope is None:
        data_as_of = "unknown"
        holdings = ""
        body = read.detail or read.reason or "unavailable"
    else:
        data_as_of = _stamp(read.envelope.data_as_of)
        symbols: list[str] = []
        if resource == "positions":
            symbols = [item.symbol for item in read.envelope.data.items]
        holdings = ",".join(symbols)
        body = read.envelope.model_dump_json()
    stale = bool(read.stale) and not unavailable
    header = "\n".join(
        [
            "trading-v1",
            f"outcome: {read.outcome.value}",
            f"stale: {str(stale).lower()}",
            f"source_engine: {source_engine}",
            f"data_as_of: {data_as_of}",
            f"resource: {resource}",
            f"holdings: {holdings}",
            "---",
        ]
    )
    return header + "\n" + body


def _parameters(engine_label: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "resource": {
                "type": "string",
                "enum": sorted(_RESOURCES),
                "description": f"Which {engine_label} read to perform. All of them are GET.",
            },
            "limit": {
                "type": "integer",
                "description": "Page size for list resources. Ignored otherwise. Max 500.",
            },
        },
        "required": ["resource"],
    }


async def _read(engine: str, resource: str, limit: int | None) -> str:
    if resource not in _RESOURCES:
        raise ToolError(f"unknown resource {resource}")
    source_engine = _ENGINES[engine][0]
    env_name = _ENGINES[engine][1]
    adapter = _resolve(engine)
    if adapter is None:
        return format_read(
            source_engine=source_engine,
            resource=resource,
            read=TradingRead(
                adapter=source_engine,
                outcome=Outcome.UNAVAILABLE,
                stale=False,
                correlation_id="",
                envelope=None,
                reason="missing_base_url",
                detail=f"{env_name} is not set",
            ),
        )
    kwargs: dict[str, Any] = {}
    if resource in _LIST_RESOURCES and limit is not None:
        kwargs["limit"] = limit
    method = getattr(adapter, resource)
    read = await method(**kwargs)
    return format_read(source_engine=source_engine, resource=resource, read=read)


@tool(
    name="trading.crypto.read",
    description=(
        "Read crypto-signal-engine. Resources: health, market_overview, signals, "
        "positions, trades, performance, risk, strategies. Read only. "
        "Paper, shadow, or research. Never an order."
    ),
    parameters=_parameters("crypto-signal-engine"),
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def trading_crypto_read(resource: str, limit: int | None = None, **_: Any) -> str:
    return await _read("crypto", resource, limit)


@tool(
    name="trading.nse.read",
    description=(
        "Read nse-trade. Resources: health, market_overview, signals, positions, "
        "trades, performance, risk, strategies. Read only. "
        "Paper, shadow, or research. Never an order."
    ),
    parameters=_parameters("nse-trade"),
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def trading_nse_read(resource: str, limit: int | None = None, **_: Any) -> str:
    return await _read("nse", resource, limit)
