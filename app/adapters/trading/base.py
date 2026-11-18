"""Shared read path for trading-v1 engines.

The JAR-011 client already refuses to raise on timeout, 5xx, and an open
circuit. This layer adds the trading envelope: source, mode, is_stale, and
the typed `data` object. `is_stale: true` marks the read stale even when the
timestamp is inside the age threshold. An old `data_as_of` stays stale even
when the engine sends `is_stale: false`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from app.adapters.client import CORRELATION_HEADER, AdapterClient
from app.adapters.models import AdapterResponse, Outcome
from app.contracts.trading_v1.models import (
    HealthResponse,
    ListQuery,
    MarketOverviewResponse,
    PerformanceResponse,
    PositionResponse,
    RiskResponse,
    SignalResponse,
    SourceEngine,
    StrategyResponse,
    TradeResponse,
)
from app.contracts.trading_v1.routes import BASE_PATH

T = TypeVar("T", bound=BaseModel)


def envelope_shell(expected_source: str):
    """Schema hook for AdapterClient. Rejects anything that is not this engine's envelope.

    Mode has no live value. A payload that claims live is invalid, not a
    quieter paper read.
    """

    def check(body: Mapping[str, Any]) -> str | None:
        if body.get("source_engine") != expected_source:
            return "unexpected source_engine"
        if body.get("schema_version") != "1.0":
            return "unsupported schema_version"
        mode = body.get("mode")
        if mode not in {"paper", "shadow", "research"}:
            return "mode must be paper, shadow, or research"
        if body.get("status") not in {"ok", "degraded", "unavailable"}:
            return "invalid status"
        if not isinstance(body.get("is_stale"), bool):
            return "is_stale must be a boolean"
        if not isinstance(body.get("correlation_id"), str) or not body.get("correlation_id"):
            return "correlation_id is required"
        if not isinstance(body.get("generated_at"), str) or not body.get("generated_at"):
            return "generated_at is required"
        if not isinstance(body.get("errors"), list):
            return "errors must be a list"
        return None

    return check


class TradingFeedClient(AdapterClient):
    """AdapterClient that keeps the full trading envelope and honors `is_stale`."""

    def _interpret(
        self, payload: Mapping[str, Any], correlation_id: str, status: int
    ) -> AdapterResponse:
        base = super()._interpret(payload, correlation_id, status)
        engine_stale = payload.get("is_stale") is True
        # Age already forced base.stale. The engine flag must also win when
        # the timestamp is still inside the threshold.
        stale = base.stale or engine_stale
        if stale and base.outcome is Outcome.OK:
            outcome = Outcome.STALE
            reason = "stale_data"
            detail = "engine set is_stale"
        else:
            outcome = base.outcome
            reason = base.reason
            detail = base.detail
        return AdapterResponse(
            adapter=self.name,
            outcome=outcome,
            correlation_id=correlation_id,
            data=dict(payload),
            data_as_of=base.data_as_of,
            stale=stale,
            reason=reason,
            detail=detail,
            http_status=status,
            checked_at=base.checked_at,
        )


@dataclass(frozen=True)
class TradingRead(Generic[T]):
    """What an agent is allowed to see. `envelope` is None when the body was unusable."""

    adapter: str
    outcome: Outcome
    stale: bool
    correlation_id: str
    envelope: T | None
    reason: str = ""
    detail: str = ""

    @property
    def data(self) -> Any:
        if self.envelope is None:
            return None
        return self.envelope.data  # type: ignore[attr-defined]


class TradingAdapter:
    """GET-only client for one engine. There is no method that writes."""

    def __init__(self, client: TradingFeedClient, source: SourceEngine) -> None:
        self.client = client
        self.source = source

    async def health(self, *, correlation_id: str | None = None) -> TradingRead[HealthResponse]:
        return await self._read(f"{BASE_PATH}/health", HealthResponse, correlation_id=correlation_id)

    async def market_overview(
        self, *, correlation_id: str | None = None
    ) -> TradingRead[MarketOverviewResponse]:
        return await self._read(
            f"{BASE_PATH}/market/overview", MarketOverviewResponse, correlation_id=correlation_id
        )

    async def signals(
        self, *, limit: int = 100, since: datetime | None = None, correlation_id: str | None = None
    ) -> TradingRead[SignalResponse]:
        return await self._list(
            f"{BASE_PATH}/signals", SignalResponse, limit=limit, since=since, correlation_id=correlation_id
        )

    async def positions(
        self, *, limit: int = 100, since: datetime | None = None, correlation_id: str | None = None
    ) -> TradingRead[PositionResponse]:
        return await self._list(
            f"{BASE_PATH}/positions",
            PositionResponse,
            limit=limit,
            since=since,
            correlation_id=correlation_id,
        )

    async def trades(
        self, *, limit: int = 100, since: datetime | None = None, correlation_id: str | None = None
    ) -> TradingRead[TradeResponse]:
        return await self._list(
            f"{BASE_PATH}/trades", TradeResponse, limit=limit, since=since, correlation_id=correlation_id
        )

    async def performance(self, *, correlation_id: str | None = None) -> TradingRead[PerformanceResponse]:
        return await self._read(
            f"{BASE_PATH}/performance", PerformanceResponse, correlation_id=correlation_id
        )

    async def risk(self, *, correlation_id: str | None = None) -> TradingRead[RiskResponse]:
        return await self._read(f"{BASE_PATH}/risk", RiskResponse, correlation_id=correlation_id)

    async def strategies(
        self, *, limit: int = 100, since: datetime | None = None, correlation_id: str | None = None
    ) -> TradingRead[StrategyResponse]:
        return await self._list(
            f"{BASE_PATH}/strategies",
            StrategyResponse,
            limit=limit,
            since=since,
            correlation_id=correlation_id,
        )

    async def _list(
        self,
        path: str,
        model: type[T],
        *,
        limit: int,
        since: datetime | None,
        correlation_id: str | None,
    ) -> TradingRead[T]:
        try:
            query = ListQuery(limit=limit, since=since)
        except ValidationError:
            return self._local_invalid("bad_query", "limit must be 1..500 and since must be timezone-aware")
        params: dict[str, Any] = {"limit": query.limit}
        if query.since is not None:
            params["since"] = query.since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await self._read(path, model, params=params, correlation_id=correlation_id)

    async def _read(
        self,
        path: str,
        model: type[T],
        *,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> TradingRead[T]:
        # GET only. The client has a general request() method; this class does not call it
        # with any other verb, so a mutation cannot be expressed from here.
        response = await self.client.get(path, params=params, correlation_id=correlation_id)
        if response.outcome in {Outcome.UNAVAILABLE, Outcome.INVALID} or not isinstance(response.data, dict):
            return TradingRead(
                adapter=self.client.name,
                outcome=response.outcome if response.outcome is not Outcome.OK else Outcome.INVALID,
                stale=False,
                correlation_id=response.correlation_id,
                envelope=None,
                reason=response.reason or "invalid_schema",
                detail=response.detail,
            )
        try:
            envelope = model.model_validate(response.data)
        except ValidationError:
            return TradingRead(
                adapter=self.client.name,
                outcome=Outcome.INVALID,
                stale=False,
                correlation_id=response.correlation_id,
                envelope=None,
                reason="invalid_schema",
                detail="response did not match trading-v1",
            )
        if envelope.correlation_id != response.correlation_id:  # type: ignore[attr-defined]
            return TradingRead(
                adapter=self.client.name,
                outcome=Outcome.INVALID,
                stale=False,
                correlation_id=response.correlation_id,
                envelope=None,
                reason="correlation_mismatch",
                detail="correlation_id was not echoed",
            )
        stale = response.stale or bool(envelope.is_stale)  # type: ignore[attr-defined]
        outcome = response.outcome
        reason = response.reason
        detail = response.detail
        status = envelope.status.value  # type: ignore[attr-defined]
        if status == "unavailable":
            outcome = Outcome.UNAVAILABLE
            reason = reason or "engine_unavailable"
        elif stale and outcome is Outcome.OK:
            outcome = Outcome.STALE
            reason = "stale_data"
        return TradingRead(
            adapter=self.client.name,
            outcome=outcome,
            stale=stale,
            correlation_id=response.correlation_id,
            envelope=envelope,
            reason=reason,
            detail=detail,
        )

    def _local_invalid(self, reason: str, detail: str) -> TradingRead[Any]:
        return TradingRead(
            adapter=self.client.name,
            outcome=Outcome.INVALID,
            stale=False,
            correlation_id="",
            envelope=None,
            reason=reason,
            detail=detail,
        )


def build_feed(
    *,
    name: str,
    base_url: str,
    source: SourceEngine,
    auth_env: str,
    stale_after: timedelta,
    **client_kwargs: Any,
) -> TradingAdapter:
    client = TradingFeedClient(
        name=name,
        base_url=base_url,
        stale_after=stale_after,
        auth_env=auth_env,
        auth_header="Authorization",
        auth_scheme="Bearer",
        schema=envelope_shell(source.value),
        **client_kwargs,
    )
    return TradingAdapter(client, source)


# Re-exported so callers can see the header name without importing the HTTP client.
CORRELATION_HEADER_NAME = CORRELATION_HEADER
