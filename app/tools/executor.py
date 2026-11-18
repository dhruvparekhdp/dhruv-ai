"""The only path that runs a tool for an agent.

The runner asks this module. This module does not trust the runner: an
unknown name is denied, the agent's grants are checked again, and an
external or dangerous action runs only when a stored approval still matches
the hash of this exact call. Prompts are not part of the decision.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from app.tools.registry import Capability, ToolError, check_permission
from app.tools.registry import registry as tool_registry

log = logging.getLogger("jarvis.tools")


async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    granted_capabilities: set[Capability],
    allowed_tools: list[str],
    mission_id: str,
    agent_id: str,
    approval_id: str | None = None,
) -> str:
    """Permission-check and run one tool call. Returns text for the model."""
    tool = tool_registry.get(tool_name)
    if tool is None:
        return f"[denied: unknown tool '{tool_name}' is not registered]"

    verdict = check_permission(
        tool,
        granted_capabilities=granted_capabilities,
        allowed_tools=allowed_tools,
    )
    if not verdict.allowed:
        log.warning("agent %s denied tool %s: %s", agent_id, tool_name, verdict.reason)
        return f"[denied: {verdict.reason}]"

    if verdict.needs_approval:
        if not approval_id:
            return f"[denied: {tool_name} requires approval]"
        # Imported here so registering a tool does not pull the database in.
        from app.missions import store as mission_store

        reason = mission_store.consume_approval(
            approval_id,
            tool_name=tool_name,
            tool_arguments=arguments,
            mission_id=mission_id,
        )
        if reason:
            log.warning("approval %s refused for %s: %s", approval_id, tool_name, reason)
            return f"[denied: {reason}]"

    # Injected after the hash check, so the model cannot put them in the
    # approved action, and cannot spoof another mission's scope.
    call = dict(arguments)
    call["_mission_id"] = mission_id
    call["_agent_id"] = agent_id

    if tool.requires_node:
        return await _dispatch_to_node(tool.name, tool.node_capability, tool.untrusted, call)

    try:
        outcome: Any = tool.handler(**call)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return str(outcome)
    except ToolError as exc:
        return f"[error: {exc}]"
    except TypeError as exc:
        return f"[error: bad arguments for {tool_name}: {exc}]"
    except Exception as exc:
        # A tool fault is data for the model. It must not kill the task.
        log.exception("tool %s raised", tool_name)
        return f"[error: {tool_name} failed: {exc}]"


async def _dispatch_to_node(
    tool_name: str,
    capability: Any,
    untrusted: bool,
    arguments: dict[str, Any],
) -> str:
    """Send a node tool to a machine in the fleet.

    Underscore-prefixed keys are orchestrator internals. The node has never
    heard of them. A missing node is reported plainly rather than run locally.
    """
    from app.nodes.gateway import NoNodeAvailable, gateway

    try:
        outcome = await gateway.dispatch(
            tool=tool_name,
            arguments={key: value for key, value in arguments.items() if not key.startswith("_")},
            capability=capability,
            untrusted=untrusted,
        )
    except NoNodeAvailable as exc:
        return f"[unavailable: {exc}]"
    except Exception as exc:
        # A node fault is data for the model. It must not kill the task.
        log.exception("node dispatch failed for %s", tool_name)
        return f"[error: {tool_name} failed on the node: {exc}]"

    if not outcome.get("ok"):
        return f"[error: {outcome.get('error') or 'node reported failure'}]"
    return str(outcome.get("output", ""))
