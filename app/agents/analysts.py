"""Analyst roster the scheduler is allowed to dispatch.

These ids are YAML files, not classes. `require_analysts` runs at registry
load so a mission cannot be planned against an analyst that failed to load,
and so none of them can pick up a write or an order tool.
"""

from __future__ import annotations

from app.tools.registry import Capability

ANALYST_AGENT_IDS: tuple[str, ...] = (
    "crypto_analyst",
    "nse_analyst",
    "portfolio_analyst",
    "risk_analyst",
    "trading_research",
)

# Exact tool lists. Adding web, memory, or shell here would let a summary
# cite something the trading adapters did not return.
ANALYST_TOOLS: dict[str, list[str]] = {
    "crypto_analyst": ["trading.crypto.read"],
    "nse_analyst": ["trading.nse.read"],
    "portfolio_analyst": ["trading.crypto.read", "trading.nse.read"],
    "risk_analyst": ["trading.crypto.read", "trading.nse.read"],
    "trading_research": ["trading.crypto.read", "trading.nse.read"],
}


def require_analysts(registry: object) -> None:
    """Raise unless every analyst is loaded, read-only, and on the summary contract."""
    from app.agents.registry import AgentDefinitionError

    missing = [agent_id for agent_id in ANALYST_AGENT_IDS if registry.get(agent_id) is None]  # type: ignore[attr-defined]
    if missing:
        raise AgentDefinitionError(f"analyst agents not registered: {missing}")

    for agent_id in ANALYST_AGENT_IDS:
        definition = registry.get(agent_id)  # type: ignore[attr-defined]
        if definition.output_contract != "trading_summary":
            raise AgentDefinitionError(f"{agent_id} must use the trading_summary contract")
        if definition.capabilities != {Capability.READ}:
            raise AgentDefinitionError(f"{agent_id} must be granted READ only")
        if list(definition.tools) != ANALYST_TOOLS[agent_id]:
            raise AgentDefinitionError(
                f"{agent_id} tools must be {ANALYST_TOOLS[agent_id]}, got {list(definition.tools)}"
            )
