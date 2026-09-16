"""WebSocket fan-out for live telemetry streaming.

Phase 1 streams turn events. The same channel carries terminal stdout, test
runs, and price ticks in later phases, which is why the payload is structured
JSON with an event type rather than raw log strings.

Hardened against two failure modes the naive implementation hits:
  * broadcasting to a client that vanished raises mid-loop and drops the event
    for every client after it -- so sends are individually guarded;
  * removing an already-removed connection raises ValueError -- so disconnect
    is idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("jarvis.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        log.info("ws client connected (%d active)", len(self.active_connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        log.info("ws client disconnected (%d active)", len(self.active_connections))

    async def broadcast(self, event: str, message: str, **fields: Any) -> None:
        """Send a structured event to every connected client.

        Dead sockets are pruned rather than allowed to break the fan-out.
        """
        payload = json.dumps(
            {
                "event": event,
                "message": message,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                **fields,
            }
        )

        async with self._lock:
            targets = list(self.active_connections)

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_text(payload)
            except Exception:  # noqa: BLE001 - client vanished mid-send
                dead.append(connection)

        for connection in dead:
            await self.disconnect(connection)


ws_manager = ConnectionManager()
