"""Process-wide adapter registry and health aggregation.

Health reports the last result and the circuit. It does not call upstreams:
a page load that probed every feed would open circuits and spend rate limit
on the thing it was trying to observe.
"""

from __future__ import annotations

from typing import Any

from app.adapters.client import AdapterClient


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AdapterClient] = {}

    def register(self, client: AdapterClient) -> AdapterClient:
        if client.name in self._adapters:
            raise ValueError(f"adapter already registered: {client.name}")
        self._adapters[client.name] = client
        return client

    def get(self, name: str) -> AdapterClient | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def all(self) -> list[AdapterClient]:
        return [self._adapters[name] for name in self.names()]

    def clear(self) -> None:
        """Drop every registration. Used by tests; the process registry is long-lived."""
        self._adapters.clear()

    def health(self) -> dict[str, Any]:
        snapshots = [client.health() for client in self.all()]
        return {
            "overall": _overall(snapshots),
            "count": len(snapshots),
            "adapters": snapshots,
        }


def _overall(snapshots: list[dict[str, Any]]) -> str:
    """Worst status wins. An open circuit is down even before the next call."""
    if not snapshots:
        return "unknown"
    if any(item["circuit"] == "open" or item["last_outcome"] == "unavailable" for item in snapshots):
        return "unavailable"
    if any(item["last_outcome"] == "invalid" for item in snapshots):
        return "invalid"
    if any(item["last_outcome"] == "stale" or item["stale"] for item in snapshots):
        return "stale"
    if any(item["last_outcome"] == "ok" for item in snapshots):
        return "ok"
    return "unknown"


registry = AdapterRegistry()
