"""The one generic agent runner.

Executes any `AgentDefinition` as a bounded tool-calling loop:

    build context -> model -> tool calls? -> permission check -> run -> repeat

Three properties matter more than the loop itself:

1. **Agents cannot call agents.** The runner exposes tools, never other agents.
   Delegation exists only as a *request* the orchestrator may refuse, which is
   what structurally prevents the uncontrolled recursion the spec warns about.

2. **Context is assembled per task**, from the task objective and its explicit
   dependency results. There is no growing global buffer, so one mission's
   context cannot leak into another's.

3. **Every tool call is permission-checked at the boundary.** The handler is
   reached only through `execute_tool`. External and dangerous calls run only
   when a stored approval still matches that exact call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.engines import EngineError, EngineRouter, ToolCall
from app.agents.registry import AgentDefinition
from app.tools.executor import execute_tool
from app.tools.registry import check_permission
from app.tools.registry import registry as tool_registry

log = logging.getLogger("jarvis.runner")

#: Hard ceiling on model round-trips, independent of the agent's budget.
#: Stops a model that keeps calling tools without ever concluding.
MAX_ITERATIONS = 8


class ApprovalRequired(Exception):
    """A dangerous tool needs a human decision before the task can continue."""

    def __init__(self, tool_name: str, arguments: dict[str, Any], risk: str) -> None:
        super().__init__(f"approval required for {tool_name}")
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk = risk


@dataclass
class RunResult:
    text: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    tool_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


#: Called with (tool_name, arguments) when an action must be approved.
#: Returns the approval id to proceed, or None to refuse. The executor
#: recomputes the action hash and consumes that row; a bare yes is not enough.
ApprovalHook = Callable[[str, dict[str, Any]], Awaitable[str | None]]


class AgentRunner:
    def __init__(self, engines: EngineRouter | None = None) -> None:
        self.engines = engines or EngineRouter()

    async def run(
        self,
        definition: AgentDefinition,
        *,
        objective: str,
        context: str = "",
        mission_id: str = "",
        approval_hook: ApprovalHook | None = None,
        on_event: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> RunResult:
        """Execute one task and return its result.

        `context` carries dependency results -- explicitly passed, never
        inherited from a shared buffer.
        """
        started = time.perf_counter()
        budget = definition.budget

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": definition.system_prompt},
            {"role": "user", "content": _build_user_message(objective, context)},
        ]
        schemas = tool_registry.schemas_for(definition.tools)

        result = RunResult(text="")

        for iteration in range(1, MAX_ITERATIONS + 1):
            result.iterations = iteration

            elapsed = time.perf_counter() - started
            if elapsed > budget.max_seconds:
                result.text = result.text or (
                    f"[stopped: exceeded the {budget.max_seconds}s time budget]"
                )
                break
            if result.total_tokens > budget.max_tokens:
                result.text = result.text or (
                    f"[stopped: exceeded the {budget.max_tokens} token budget]"
                )
                break

            try:
                completion = await asyncio.to_thread(
                    self.engines.chat,
                    messages=messages,
                    tools=schemas or None,
                    prefer=definition.model_preference,
                )
            except EngineError as exc:
                raise RuntimeError(f"all engines failed: {exc}") from exc

            result.provider = completion.provider
            result.model = completion.model
            result.prompt_tokens += completion.prompt_tokens
            result.completion_tokens += completion.completion_tokens

            if not completion.wants_tools:
                result.text = completion.text
                break

            # Record the assistant turn verbatim so the model sees its own call.
            messages.append(
                {
                    "role": "assistant",
                    "content": completion.text or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in completion.tool_calls
                    ],
                }
            )

            for call in completion.tool_calls:
                if result.tool_calls >= budget.max_tool_calls:
                    messages.append(_tool_message(call, "[refused: tool-call budget exhausted]"))
                    continue

                result.tool_calls += 1
                output = await self._invoke(
                    definition,
                    call,
                    mission_id=mission_id,
                    approval_hook=approval_hook,
                    on_event=on_event,
                )
                result.tool_log.append(
                    {"tool": call.name, "arguments": call.arguments, "output": output[:500]}
                )
                messages.append(_tool_message(call, output))
        else:
            # Loop ran to MAX_ITERATIONS without the model concluding.
            result.text = result.text or "[stopped: too many tool-calling rounds without an answer]"

        if not result.text:
            result.text = "[no answer produced]"
        return result

    async def _invoke(
        self,
        definition: AgentDefinition,
        call: ToolCall,
        *,
        mission_id: str,
        approval_hook: ApprovalHook | None,
        on_event: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None,
    ) -> str:
        """Ask the executor to run one call. The handler is not reached from here."""
        tool = tool_registry.get(call.name)
        if tool is None:
            return f"[denied: unknown tool '{call.name}' is not registered]"

        verdict = check_permission(
            tool,
            granted_capabilities=definition.capabilities,
            allowed_tools=definition.tools,
        )
        if not verdict.allowed:
            log.warning("agent %s denied tool %s: %s", definition.id, call.name, verdict.reason)
            if on_event:
                await on_event("permission", f"Denied {call.name}: {verdict.reason}", {})
            return f"[denied: {verdict.reason}]"

        approval_id: str | None = None
        if verdict.needs_approval:
            if approval_hook is None:
                return "[denied: this tool requires human approval, which is unavailable here]"
            granted = await approval_hook(call.name, call.arguments)
            if not isinstance(granted, str) or not granted:
                return f"[denied: the user did not approve {call.name}]"
            approval_id = granted

        if on_event:
            await on_event("tool", f"{definition.id} → {call.name}", {"arguments": call.arguments})

        return await execute_tool(
            call.name,
            dict(call.arguments),
            granted_capabilities=definition.capabilities,
            allowed_tools=definition.tools,
            mission_id=mission_id,
            agent_id=definition.id,
            approval_id=approval_id,
        )


def _build_user_message(objective: str, context: str) -> str:
    if not context:
        return objective
    return (
        f"{objective}\n\n"
        f"--- Results from earlier tasks in this mission ---\n{context}"
    )


def _tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
    }
