"""Deny-by-default policy for a tool call.

Levels describe the action, not the agent that asked:

  read             look at state that already exists
  internal_write   change state that stays inside Jarvis
  external         send anything off this machine (the network)
  dangerous        hard to undo, or run code the model produced

`external` and `dangerous` do not run until a stored approval matches the hash
of this exact call. A name that is not in the registry is denied. There is no
default allow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any


class PolicyLevel(str, Enum):
    READ = "read"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL = "external"
    DANGEROUS = "dangerous"

    @property
    def requires_approval(self) -> bool:
        """True when the handler must not run on an agent's say-so."""
        return self in {PolicyLevel.EXTERNAL, PolicyLevel.DANGEROUS}


def canonical_params(params: Mapping[str, Any]) -> Any:
    """Stable JSON value for a tool call.

    Keys starting with `_` are orchestrator internals (`_mission_id`,
    `_agent_id`). They are injected after the approval is checked, so they
    are not part of the action the human approved. Nested objects are sorted;
    list order is kept, because order can change what the tool does.
    """
    visible = {key: value for key, value in params.items() if not str(key).startswith("_")}
    return _canonical(visible)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"tool params must be JSON values, got {type(value).__name__}")


def action_hash(tool_name: str, params: Mapping[str, Any]) -> str:
    """SHA-256 of the tool name plus its canonical params.

    The stored approval keeps this digest. Execution recomputes it from the
    call that is about to run and refuses the call when the two differ.
    """
    body = json.dumps(
        {"params": canonical_params(params), "tool": tool_name},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
