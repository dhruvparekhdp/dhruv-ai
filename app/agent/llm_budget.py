"""Per-provider cooldown — skip engines that are about to 429."""

from __future__ import annotations

import time
from threading import Lock

_lock = Lock()
_cooldown_until: dict[str, float] = {}


def mark_cooled(provider: str, seconds: float) -> None:
    if seconds <= 0:
        return
    until = time.monotonic() + seconds
    with _lock:
        current = _cooldown_until.get(provider.upper(), 0.0)
        _cooldown_until[provider.upper()] = max(current, until)


def is_available(provider: str) -> bool:
    with _lock:
        until = _cooldown_until.get(provider.upper(), 0.0)
    return time.monotonic() >= until


def clear(provider: str | None = None) -> None:
    with _lock:
        if provider is None:
            _cooldown_until.clear()
        else:
            _cooldown_until.pop(provider.upper(), None)
