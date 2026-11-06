"""Pydantic models for trading-v1.

Prices, quantities, ratios, and money amounts are decimal strings. JSON
numbers are rejected so a float cannot round a price. Money objects carry
an explicit currency code.

Mode is paper, shadow, or research. There is no live value to set.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Generic, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

SCHEMA_VERSION = "1.0"

# No leading zeros, no exponent, optional fraction. A leading minus is the
# sign for PnL; prices use the same shape and are checked by the fixtures
# and by callers that require a non-negative book.
SignedDecimal = Annotated[str, Field(pattern=r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")]
UnsignedDecimal = Annotated[str, Field(pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?$")]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3,10}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class SourceEngine(str, Enum):
    CRYPTO = "crypto-signal-engine"
    NSE = "nse-trade"


class FeedStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Mode(str, Enum):
    """Execution stance the engine is reporting. Live is intentionally absent."""

    PAPER = "paper"
    SHADOW = "shadow"
    RESEARCH = "research"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    INVALIDATED = "invalidated"


class Book(str, Enum):
    MIS = "MIS"
    CNC = "CNC"
    SPOT = "spot"
    FUTURES = "futures"


class StrategyStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class ApiError(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)


class Money(StrictModel):
    amount: SignedDecimal
    currency: CurrencyCode


class Page(StrictModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None

    @field_validator("next_cursor")
    @classmethod
    def _cursor(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("next_cursor must be null or non-empty")
        return value


class Signal(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    strength: UnsignedDecimal
    strategy: str = Field(min_length=1, max_length=80)
    strategy_version: str = Field(min_length=1, max_length=40)
    generated_at: AwareDatetime
    status: SignalStatus
    rationale: str = Field(max_length=2000)

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class Position(StrictModel):
    symbol: str = Field(min_length=1, max_length=40)
    qty: UnsignedDecimal
    avg_price: Money
    mark_price: Money
    unrealized_pnl: Money
    book: Book
    opened_at: AwareDatetime

    @field_validator("opened_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class Trade(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=40)
    side: Side
    qty: UnsignedDecimal
    price: Money
    fees: Money
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None
    pnl: Money | None
    strategy: str = Field(min_length=1, max_length=80)
    mode: Mode

    @field_validator("opened_at", "closed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)


class PerformancePeriod(StrictModel):
    label: str = Field(min_length=1, max_length=40)
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class Performance(StrictModel):
    period: PerformancePeriod
    trades_count: int = Field(ge=0)
    win_rate: UnsignedDecimal
    expectancy: Money
    max_drawdown: Money
    sharpe: SignedDecimal | None


class Exposure(StrictModel):
    kind: str = Field(pattern=r"^(symbol|sector)$")
    name: str = Field(min_length=1, max_length=40)
    amount: Money


class ConfiguredLimit(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    max_notional: Money | None = None
    max_leverage: UnsignedDecimal | None = None


class LimitBreach(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    limit_notional: Money | None = None
    actual_notional: Money | None = None
    limit_leverage: UnsignedDecimal | None = None
    actual_leverage: UnsignedDecimal | None = None


class RiskSnapshot(StrictModel):
    exposure: list[Exposure]
    leverage: UnsignedDecimal
    limits: list[ConfiguredLimit]
    breaches: list[LimitBreach]


class Strategy(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    status: StrategyStatus
    params_summary: str = Field(max_length=500)


class HealthCheck(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    status: FeedStatus
    detail: str = Field(default="", max_length=300)


class HealthData(StrictModel):
    """Typed health body. Extra notes are strings in `details`, never numbers."""

    service: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    checks: list[HealthCheck]
    details: dict[str, str] = Field(default_factory=dict)


class MarketHighlight(StrictModel):
    symbol: str = Field(min_length=1, max_length=40)
    last: Money
    change_pct: SignedDecimal


class MarketOverview(StrictModel):
    """Typed overview. Free text lives in `details` so floats cannot hide there."""

    venue: str = Field(min_length=1, max_length=40)
    instruments_tracked: int = Field(ge=0)
    highlights: list[MarketHighlight]
    details: dict[str, str] = Field(default_factory=dict)


class Envelope(StrictModel, Generic[T]):
    source_engine: SourceEngine
    schema_version: str = Field(pattern=r"^1\.0$")
    generated_at: AwareDatetime
    data_as_of: AwareDatetime
    status: FeedStatus
    is_stale: bool
    mode: Mode
    correlation_id: str = Field(min_length=1, max_length=200)
    data: T
    errors: list[ApiError]

    @field_validator("generated_at", "data_as_of")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ListQuery(StrictModel):
    """Query string for list routes. `limit` stops at 500."""

    limit: int = Field(default=100, ge=1, le=500)
    since: AwareDatetime | None = None

    @field_validator("since")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)


# Concrete response models. Generics are resolved here so JSON Schema export
# and the adapters share one object.
SignalResponse = Envelope[Page[Signal]]
PositionResponse = Envelope[Page[Position]]
TradeResponse = Envelope[Page[Trade]]
StrategyResponse = Envelope[Page[Strategy]]
PerformanceResponse = Envelope[Performance]
RiskResponse = Envelope[RiskSnapshot]
HealthResponse = Envelope[HealthData]
MarketOverviewResponse = Envelope[MarketOverview]

RESPONSE_MODELS: dict[str, type[StrictModel]] = {
    "/integration/v1/health": HealthResponse,
    "/integration/v1/market/overview": MarketOverviewResponse,
    "/integration/v1/signals": SignalResponse,
    "/integration/v1/positions": PositionResponse,
    "/integration/v1/trades": TradeResponse,
    "/integration/v1/performance": PerformanceResponse,
    "/integration/v1/risk": RiskResponse,
    "/integration/v1/strategies": StrategyResponse,
}

PAYLOAD_MODELS: dict[str, type[StrictModel]] = {
    "signal": Signal,
    "position": Position,
    "trade": Trade,
    "performance": Performance,
    "risk": RiskSnapshot,
    "strategy": Strategy,
    "health": HealthData,
    "market-overview": MarketOverview,
    "list-query": ListQuery,
    "money": Money,
    "error": ApiError,
}
