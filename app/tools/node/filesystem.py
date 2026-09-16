"""Filesystem tools, executed on a node.

The node enforces the real boundary: every path is resolved and checked against
its configured roots, so `../` cannot escape. The declarations here set the
permission and risk policy.
"""

from __future__ import annotations

from app.nodes.models import NodeCapability
from app.tools.node._base import not_local
from app.tools.registry import Capability, Risk, tool

_PATH = {"type": "string", "description": "Path on the node, inside its allowed roots."}


tool(
    name="fs.list",
    description="List a directory on a node. Returns names, sizes, and which entries are directories.",
    parameters={"type": "object", "properties": {"path": _PATH}, "required": []},
    capability=Capability.READ,
    risk=Risk.SAFE,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)

tool(
    name="fs.read",
    description="Read a UTF-8 text file from a node. Large files are truncated.",
    parameters={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    capability=Capability.READ,
    risk=Risk.SAFE,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)

tool(
    name="fs.write",
    description="Write a UTF-8 text file on a node, creating parent directories as needed.",
    parameters={
        "type": "object",
        "properties": {"path": _PATH, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    capability=Capability.WRITE,
    risk=Risk.SENSITIVE,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)

tool(
    name="fs.delete",
    description="Permanently delete a file on a node. This cannot be undone.",
    parameters={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    capability=Capability.WRITE,
    # Irreversible and on real hardware, so it always parks for approval.
    risk=Risk.DANGEROUS,
    requires_node=True,
    node_capability=NodeCapability.EXECUTE,
)(not_local)
