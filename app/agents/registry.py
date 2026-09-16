"""Agent registry -- agents are records, not classes.

The spec calls for ~20 specialist agents. Twenty Python classes would be
unmaintainable by the time the fleet arrives, and every one of them would
re-implement the same tool-calling loop slightly differently.

So an agent is a YAML file: identity, system prompt, allowed tools, capability
grants, model preference, budget. One generic runner (`agents/runner.py`)
executes any of them. Adding an agent is adding a file.

Definitions are validated at load time -- a typo in a tool name or capability
fails at startup, loudly, rather than halfway through a mission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.missions.models import Budget
from app.tools.registry import Capability, registry as tool_registry

log = logging.getLogger("jarvis.agents")

DEFINITIONS_DIR = Path(__file__).parent / "definitions"

#: Agent used for plain conversation and as the fallback assignment.
DEFAULT_AGENT_ID = "assistant"
#: Agent that decomposes goals into task graphs.
PLANNER_AGENT_ID = "planner"


class AgentDefinitionError(ValueError):
    """A definition file is malformed or references something unknown."""


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    capabilities: set[Capability] = field(default_factory=set)
    model_preference: str = "fast"     # "fast" -> Groq, "deep" -> Gemini
    budget: Budget = field(default_factory=Budget)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "capabilities": sorted(c.value for c in self.capabilities),
            "model_preference": self.model_preference,
            "budget": self.budget.as_dict(),
        }


def _parse(raw: dict[str, Any], source: str) -> AgentDefinition:
    for required in ("id", "name", "system_prompt"):
        if not raw.get(required):
            raise AgentDefinitionError(f"{source}: missing required field '{required}'")

    tools = list(raw.get("tools") or [])
    unknown = [name for name in tools if tool_registry.get(name) is None]
    if unknown:
        raise AgentDefinitionError(
            f"{source}: references unknown tool(s) {unknown}. "
            f"Known tools: {tool_registry.names()}"
        )

    capabilities: set[Capability] = set()
    for raw_capability in raw.get("capabilities") or []:
        try:
            capabilities.add(Capability(raw_capability))
        except ValueError as exc:
            raise AgentDefinitionError(
                f"{source}: unknown capability '{raw_capability}'. "
                f"Valid: {[c.value for c in Capability]}"
            ) from exc

    # Catch the dangerous mismatch: an agent listing a tool it can never call.
    for name in tools:
        tool = tool_registry.get(name)
        assert tool is not None  # checked above
        if tool.capability not in capabilities:
            raise AgentDefinitionError(
                f"{source}: lists tool '{name}' which needs capability "
                f"{tool.capability.value}, but the agent is not granted it"
            )

    preference = raw.get("model_preference", "fast")
    if preference not in ("fast", "deep"):
        raise AgentDefinitionError(f"{source}: model_preference must be 'fast' or 'deep'")

    return AgentDefinition(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        system_prompt=raw["system_prompt"].strip(),
        tools=tools,
        capabilities=capabilities,
        model_preference=preference,
        budget=Budget.from_dict(raw.get("budget")),
    )


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def load(self, directory: Path | None = None) -> "AgentRegistry":
        """Load and validate every definition. Raises on the first bad file."""
        directory = directory or DEFINITIONS_DIR
        self._agents.clear()

        for path in sorted(directory.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise AgentDefinitionError(f"{path.name}: expected a YAML mapping")
            definition = _parse(raw, path.name)
            if definition.id in self._agents:
                raise AgentDefinitionError(f"{path.name}: duplicate agent id '{definition.id}'")
            self._agents[definition.id] = definition

        missing = [a for a in (DEFAULT_AGENT_ID, PLANNER_AGENT_ID) if a not in self._agents]
        if missing:
            raise AgentDefinitionError(f"required agent definition(s) missing: {missing}")

        log.info("loaded %d agents: %s", len(self._agents), ", ".join(sorted(self._agents)))
        return self

    def get(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    def ids(self) -> list[str]:
        return sorted(self._agents)

    def all(self) -> list[AgentDefinition]:
        return [self._agents[i] for i in self.ids()]

    def roster_for_planner(self) -> str:
        """Compact agent list injected into the planner's prompt.

        Excludes the planner itself -- it must never assign work to itself.
        """
        lines = []
        for definition in self.all():
            if definition.id == PLANNER_AGENT_ID:
                continue
            tools = ", ".join(definition.tools) if definition.tools else "no tools"
            lines.append(f"- {definition.id}: {definition.description} (can use: {tools})")
        return "\n".join(lines)


registry = AgentRegistry()
