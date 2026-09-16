"""Tool registry -- where capability and risk are actually enforced.

Agents decide; tools execute. Permission checks therefore belong here, at the
tool boundary, not on the agent: the danger lives in what gets done, not in who
asked for it. An agent with no granted capabilities is harmless no matter what
the model produces.

Every tool declares:
  * a JSON schema, handed to the model for tool-calling
  * a capability, which an agent must hold to call it
  * a risk level, which decides whether it runs, is audited, or parks for a
    human decision
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from app.nodes.models import NodeCapability


class Capability(str, Enum):
    READ = "READ"          # read-only information gathering
    WRITE = "WRITE"        # mutate stored state (memory, files)
    EXECUTE = "EXECUTE"    # run something on a device
    FINANCIAL = "FINANCIAL"


class Risk(str, Enum):
    SAFE = "SAFE"              # runs
    SENSITIVE = "SENSITIVE"    # runs if granted; always audited
    DANGEROUS = "DANGEROUS"    # always parks for human approval


class ToolError(RuntimeError):
    """Raised by a tool when it fails in an expected way.

    Returned to the model as an error result so it can adapt, rather than
    crashing the task.
    """


class ToolHandler(Protocol):
    def __call__(self, **kwargs: Any) -> Awaitable[str]: ...


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema for the arguments object
    capability: Capability
    risk: Risk
    handler: ToolHandler
    #: Tools that must run on a device node rather than in-process.
    requires_node: bool = False
    #: Which node capability the work needs. Placement is by capability, never
    #: by hostname (D-023).
    node_capability: NodeCapability = NodeCapability.EXECUTE
    #: Whether this must run on a node explicitly cleared for untrusted,
    #: model-generated code.
    untrusted: bool = False

    def schema(self) -> dict[str, Any]:
        """OpenAI/Groq-style function schema. Gemini accepts the same shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas_for(self, names: list[str]) -> list[dict[str, Any]]:
        """Schemas for the tools an agent is allowed to see.

        An agent is never shown a tool it cannot call -- withholding it from the
        prompt prevents most permission errors before they happen.
        """
        return [self._tools[n].schema() for n in names if n in self._tools]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "capability": tool.capability.value,
                "risk": tool.risk.value,
                "requires_node": tool.requires_node,
                "node_capability": tool.node_capability.value if tool.requires_node else None,
                "untrusted": tool.untrusted,
            }
            for tool in (self._tools[n] for n in self.names())
        ]


@dataclass
class PermissionVerdict:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""


def check_permission(
    tool: Tool,
    *,
    granted_capabilities: set[Capability],
    allowed_tools: list[str],
) -> PermissionVerdict:
    """Decide whether an agent may run a tool, and whether a human must confirm.

    Both gates must pass: the agent's explicit tool allowlist and its capability
    grants. The allowlist alone is not enough -- capabilities are what stop a
    typo in a YAML file from handing an agent the ability to spend money.
    """
    if tool.name not in allowed_tools:
        return PermissionVerdict(False, reason=f"'{tool.name}' is not in this agent's toolset")

    if tool.capability not in granted_capabilities:
        return PermissionVerdict(
            False,
            reason=f"'{tool.name}' requires {tool.capability.value}, which this agent lacks",
        )

    if tool.risk is Risk.DANGEROUS:
        return PermissionVerdict(True, needs_approval=True, reason="dangerous tool requires approval")

    return PermissionVerdict(True)


#: Process-wide registry. Tool modules register into this on import.
registry = ToolRegistry()


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    capability: Capability,
    risk: Risk,
    requires_node: bool = False,
    node_capability: NodeCapability = NodeCapability.EXECUTE,
    untrusted: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator registering an async function as a tool."""

    def decorator(handler: ToolHandler) -> ToolHandler:
        registry.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                capability=capability,
                risk=risk,
                handler=handler,
                requires_node=requires_node,
                node_capability=node_capability,
                untrusted=untrusted,
            )
        )
        return handler

    return decorator
