"""trading-v1 models, exported schemas, and the read-only route table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.trading_v1.auth import bearer_matches, parse_bearer
from app.contracts.trading_v1.export import CONTRACT_DIR, documents, render
from app.contracts.trading_v1.models import (
    RESPONSE_MODELS,
    Signal,
    Trade,
)
from app.contracts.trading_v1.routes import PATHS, decide
from tests.jsonschema_subset import SchemaError, validate_instance

FIXTURES = CONTRACT_DIR / "fixtures"
TOKEN = "test-integration-token"

PATH_FOR = {
    "health.json": "/integration/v1/health",
    "market-overview.json": "/integration/v1/market/overview",
    "signals.json": "/integration/v1/signals",
    "signals-research.json": "/integration/v1/signals",
    "positions.json": "/integration/v1/positions",
    "trades.json": "/integration/v1/trades",
    "performance.json": "/integration/v1/performance",
    "risk.json": "/integration/v1/risk",
    "strategies.json": "/integration/v1/strategies",
}

SCHEMA_FOR = {
    "health.json": "health.response.schema.json",
    "market-overview.json": "market-overview.response.schema.json",
    "signals.json": "signal.response.schema.json",
    "signals-research.json": "signal.response.schema.json",
    "positions.json": "position.response.schema.json",
    "trades.json": "trade.response.schema.json",
    "performance.json": "performance.response.schema.json",
    "risk.json": "risk.response.schema.json",
    "strategies.json": "strategy.response.schema.json",
}


def test_exported_schemas_match_the_models() -> None:
    fresh = documents()
    for filename, document in fresh.items():
        on_disk = (CONTRACT_DIR / filename).read_text(encoding="utf-8")
        assert on_disk == render(document), filename


def test_fixtures_match_models_and_schemas() -> None:
    schemas = documents()
    files = sorted(FIXTURES.glob("*/*.json"))
    assert len(files) >= 16
    modes = set()
    for path in files:
        body = json.loads(path.read_text(encoding="utf-8"))
        validate_instance(body, schemas[SCHEMA_FOR[path.name]])
        model = RESPONSE_MODELS[PATH_FOR[path.name]].model_validate(body)
        modes.add(model.mode.value)
        assert model.schema_version == "1.0"
        assert model.source_engine.value in {"crypto-signal-engine", "nse-trade"}
    assert modes == {"paper", "shadow", "research"}


def test_prices_are_decimal_strings_not_floats() -> None:
    payload = json.loads((FIXTURES / "crypto" / "signals.json").read_text(encoding="utf-8"))
    payload["data"]["items"][0]["strength"] = 0.72
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["/integration/v1/signals"].model_validate(payload)
    with pytest.raises(SchemaError):
        validate_instance(payload, documents()["signal.response.schema.json"])


def test_live_mode_is_rejected() -> None:
    payload = json.loads((FIXTURES / "crypto" / "health.json").read_text(encoding="utf-8"))
    payload["mode"] = "live"
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["/integration/v1/health"].model_validate(payload)


def test_extra_fields_and_empty_cursor_are_rejected() -> None:
    payload = json.loads((FIXTURES / "crypto" / "signals.json").read_text(encoding="utf-8"))
    payload["order_id"] = "nope"
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["/integration/v1/signals"].model_validate(payload)
    payload = json.loads((FIXTURES / "crypto" / "signals.json").read_text(encoding="utf-8"))
    payload["data"]["next_cursor"] = "  "
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["/integration/v1/signals"].model_validate(payload)


def test_open_trade_allows_null_close_and_pnl() -> None:
    payload = json.loads((FIXTURES / "crypto" / "trades.json").read_text(encoding="utf-8"))
    trades = RESPONSE_MODELS["/integration/v1/trades"].model_validate(payload).data.items
    closed = next(item for item in trades if item.id == "trd-btc-closed")
    opened = next(item for item in trades if item.id == "trd-btc-open")
    assert closed.pnl is not None and closed.pnl.amount.startswith("-")
    assert opened.closed_at is None and opened.pnl is None


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Signal.model_validate(
            {
                "id": "sig-1",
                "symbol": "BTCUSDT",
                "side": "buy",
                "strength": "0.1",
                "strategy": "momentum",
                "strategy_version": "3",
                "generated_at": "2026-11-06T11:58:00",
                "status": "active",
                "rationale": "no zone",
            }
        )


def test_only_get_is_allowed_and_there_is_no_order_route() -> None:
    for path in PATHS:
        assert decide("GET", path).status_code == 200
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert decide(method, path).status_code == 405
    assert decide("GET", "/integration/v1/orders").status_code == 404
    assert decide("POST", "/integration/v1/orders").status_code == 405
    assert decide("POST", "/integration/v1/trades").status_code == 405


def test_bearer_compare_is_constant_time_and_fails_closed() -> None:
    source = Path("app/contracts/trading_v1/auth.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert parse_bearer(f"Bearer {TOKEN}") == TOKEN
    assert parse_bearer(f"bearer {TOKEN}") is None
    assert bearer_matches(TOKEN, TOKEN) is True
    assert bearer_matches(TOKEN, "other-token") is False
    assert bearer_matches(TOKEN, "") is False
    assert bearer_matches(TOKEN, None) is False
    assert bearer_matches(None, TOKEN) is False


def test_trade_mode_cannot_be_live() -> None:
    with pytest.raises(ValidationError):
        Trade.model_validate(
            {
                "id": "trd-1",
                "symbol": "BTCUSDT",
                "side": "buy",
                "qty": "1",
                "price": {"amount": "1.00", "currency": "USDT"},
                "fees": {"amount": "0.01", "currency": "USDT"},
                "opened_at": "2026-11-06T09:15:00Z",
                "closed_at": None,
                "pnl": None,
                "strategy": "momentum",
                "mode": "live",
            }
        )
