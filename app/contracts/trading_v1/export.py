"""Write the trading-v1 JSON Schemas next to the contract docs.

Each file is self-contained ($defs included) so a consumer does not need this
repository to validate a payload. Key order is sorted so a regen is diff-stable.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.trading_v1.models import (
    PAYLOAD_MODELS,
    HealthResponse,
    MarketOverviewResponse,
    PerformanceResponse,
    PositionResponse,
    RiskResponse,
    SignalResponse,
    StrategyResponse,
    TradeResponse,
)

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "trading-v1"

RESPONSE_SCHEMAS: dict[str, type] = {
    "signal.response.schema.json": SignalResponse,
    "position.response.schema.json": PositionResponse,
    "trade.response.schema.json": TradeResponse,
    "strategy.response.schema.json": StrategyResponse,
    "performance.response.schema.json": PerformanceResponse,
    "risk.response.schema.json": RiskResponse,
    "health.response.schema.json": HealthResponse,
    "market-overview.response.schema.json": MarketOverviewResponse,
}


def documents() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for name, model in PAYLOAD_MODELS.items():
        docs[f"{name}.schema.json"] = model.model_json_schema()
    for filename, model in RESPONSE_SCHEMAS.items():
        docs[filename] = model.model_json_schema()
    return docs


def render(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def export_schemas(directory: Path | None = None) -> Path:
    target = directory or CONTRACT_DIR
    target.mkdir(parents=True, exist_ok=True)
    for filename, document in documents().items():
        (target / filename).write_text(render(document), encoding="utf-8")
    return target
