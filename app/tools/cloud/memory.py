"""Memory tools.

Thin wrappers over `app.memory.store` and `app.memory.search`. Memory is
reached only through these permissioned, audited calls -- never as shared state
an agent can read directly. That is what keeps one mission's context out of
another's.
"""

from __future__ import annotations

import json
from typing import Any

from app.memory import search as memory_search
from app.memory import store
from app.memory.store import MemoryError_, VALID_SCOPES
from app.tools.registry import Capability, Risk, ToolError, tool

# Re-exported so callers keep a single import for schema setup.
init_db = store.init_db


def _scope_key(scope: str, mission_id: str) -> str:
    """Mission scope is partitioned per mission; other scopes are global."""
    return mission_id if scope == "mission" else ""


@tool(
    name="memory.search",
    description=(
        "Search memory by meaning. Use this when you need what you know about a topic but "
        "do not know the exact key -- it matches paraphrase as well as literal wording. "
        "Prefer this over memory.read for open questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you are looking for."},
            "scope": {
                "type": "string",
                "enum": list(VALID_SCOPES),
                "description": "Optional. Omit to search every scope.",
            },
        },
        "required": ["query"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def memory_search_tool(
    query: str, scope: str = "", _mission_id: str = "", **_: Any
) -> str:
    try:
        outcome = memory_search.search(
            query, scope=scope, scope_key=_scope_key(scope, _mission_id)
        )
    except MemoryError_ as exc:
        raise ToolError(str(exc)) from exc

    if not outcome["results"]:
        return f"Nothing in memory matches '{query}'."

    lines = []
    for item in outcome["results"]:
        learned = (item.get("created_at") or "")[:10]
        lines.append(f"- [{item['scope']}] {item['mem_key']}: {item['value']} (learned {learned})")

    # Say when semantic matching was unavailable, so a thin result set is not
    # mistaken for an empty memory.
    footer = "" if outcome["semantic"] else "\n(keyword-only: semantic search unavailable)"
    return "\n".join(lines) + footer


@tool(
    name="memory.read",
    description=(
        "Read stored facts by exact key. Use scope 'user' for facts about the user, "
        "'project' for facts about this build, 'mission' for notes from the current mission. "
        "Omit 'key' to list a scope. For open questions use memory.search instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": list(VALID_SCOPES)},
            "key": {"type": "string", "description": "Optional exact key."},
        },
        "required": ["scope"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def memory_read_tool(scope: str, key: str = "", _mission_id: str = "", **_: Any) -> str:
    try:
        entries = store.read(scope, key, scope_key=_scope_key(scope, _mission_id))
    except MemoryError_ as exc:
        raise ToolError(str(exc)) from exc

    if not entries:
        return f"No entries in {scope} memory" + (f" for key '{key}'." if key else ".")
    return json.dumps(
        [{"key": e["mem_key"], "value": e["value"], "updated": e["updated_at"]} for e in entries],
        ensure_ascii=False,
    )


@tool(
    name="memory.write",
    description=(
        "Store a durable fact. Keep values short -- store conclusions, not transcripts. "
        "Prefer updating an existing key over creating a near-duplicate."
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": list(VALID_SCOPES)},
            "key": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["scope", "key", "value"],
    },
    capability=Capability.WRITE,
    risk=Risk.SENSITIVE,
)
async def memory_write_tool(
    scope: str, key: str, value: str, _mission_id: str = "", _agent_id: str = "", **_: Any
) -> str:
    try:
        store.write(
            scope, key, value,
            scope_key=_scope_key(scope, _mission_id),
            source=f"agent:{_agent_id}" if _agent_id else "agent",
        )
    except MemoryError_ as exc:
        raise ToolError(str(exc)) from exc
    return f"Stored '{key}' in {scope} memory."


@tool(
    name="memory.forget",
    description="Permanently delete a stored fact. This cannot be undone.",
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": list(VALID_SCOPES)},
            "key": {"type": "string"},
        },
        "required": ["scope", "key"],
    },
    capability=Capability.WRITE,
    # Irreversible, so it always parks for a human decision regardless of grants.
    risk=Risk.DANGEROUS,
)
async def memory_forget_tool(scope: str, key: str, _mission_id: str = "", **_: Any) -> str:
    try:
        deleted = store.forget(scope, key, scope_key=_scope_key(scope, _mission_id))
    except MemoryError_ as exc:
        raise ToolError(str(exc)) from exc
    if not deleted:
        return f"No entry '{key}' in {scope} memory; nothing deleted."
    return f"Deleted '{key}' from {scope} memory."
