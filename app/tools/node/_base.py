"""Shared plumbing for node tools.

A node tool is a *declaration*: schema, capability, risk. The body never runs
here -- `AgentRunner` sees `requires_node` and routes the call to the gateway
instead. This handler exists only so a direct call fails loudly rather than
silently doing nothing.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolError


async def not_local(**kwargs: Any) -> str:
    raise ToolError(
        "this tool runs on a node and must be dispatched through the gateway, "
        "not invoked in-process"
    )
