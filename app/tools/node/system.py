"""Shell and system tools, executed on a node."""

from __future__ import annotations

from app.nodes.models import NodeCapability
from app.tools.node._base import not_local
from app.tools.registry import Capability, Risk, tool

tool(
    name="system.stats",
    description=(
        "Read a node's health: platform, CPU cores, RAM available, disk free, load average."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    capability=Capability.READ,
    risk=Risk.SAFE,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)

tool(
    name="shell.exec",
    description=(
        "Run a shell command on a node and return its exit code, stdout and stderr. "
        "The node enforces its own allowlist, so a command may still be refused."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed."},
        },
        "required": ["command"],
    },
    capability=Capability.EXECUTE,
    # Arbitrary execution on real hardware always needs a human decision.
    risk=Risk.DANGEROUS,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)
