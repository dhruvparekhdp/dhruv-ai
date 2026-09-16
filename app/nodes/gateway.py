"""Node gateway: live connections, placement, and dispatch.

Nodes dial **out** to the orchestrator and hold the socket (D-014) — nothing
dials in, because every machine sits behind NAT. Commands travel down the
established connection and results come back up it.

Placement is by capability, never by hostname or IP (D-023). Ask for a node
that can `EXECUTE`; the registry finds one, or reports honestly that none is
available.

Protocol (JSON lines over one WebSocket):

    node -> server   hello        join with an enrolment token, or reconnect with node_id + secret
                     heartbeat    liveness plus metrics (CPU, RAM, battery...)
                     result       outcome of a dispatched call
    server -> node   welcome      node_id, and the secret on first enrolment
                     dispatch     run this tool with these arguments
                     error        what went wrong with the last message
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from app.nodes import store
from app.nodes.models import (
    HEARTBEAT_TIMEOUT_SECONDS,
    Node,
    NodeCapability,
    NodeStatus,
    parse_capabilities,
)

log = logging.getLogger("jarvis.nodes")

#: How long to wait for a node to answer a dispatched call before giving up.
#: The task lease outlives this, so a timeout requeues rather than losing work.
DEFAULT_DISPATCH_TIMEOUT = 60.0


class NoNodeAvailable(RuntimeError):
    """No connected node satisfies the request.

    Raised rather than silently falling back, so the user is told the fleet
    cannot do the thing instead of being handed a fabricated result.
    """


class NodeGateway:
    def __init__(self) -> None:
        self._sockets: dict[str, WebSocket] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    # -- connection lifecycle -------------------------------------------

    async def attach(self, node_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            existing = self._sockets.get(node_id)
            self._sockets[node_id] = websocket
        if existing is not None:
            # A node reconnecting before the old socket timed out; drop the stale one.
            try:
                await existing.close()
            except Exception:  # noqa: BLE001
                pass
        store.update_presence(node_id, status=NodeStatus.ONLINE)
        log.info("node online: %s (%d connected)", node_id, len(self._sockets))

    async def detach(self, node_id: str) -> None:
        async with self._lock:
            self._sockets.pop(node_id, None)
        store.update_presence(node_id, status=NodeStatus.OFFLINE, clear_task=True)
        log.info("node offline: %s (%d connected)", node_id, len(self._sockets))

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._sockets

    def connected_ids(self) -> list[str]:
        return sorted(self._sockets)

    # -- placement -------------------------------------------------------

    def select(
        self, capability: NodeCapability, *, untrusted: bool = False
    ) -> Node | None:
        """Pick a connected node that can do the work.

        Untrusted execution requires an explicit opt-in on the node. If none is
        designated, this returns None rather than quietly running model-generated
        code on the machine holding the database.
        """
        candidates: list[Node] = []
        for node_id in self.connected_ids():
            node = store.get(node_id)
            if node is None or node.status is not NodeStatus.ONLINE:
                continue
            if not node.has(capability):
                continue
            if untrusted and not node.untrusted_ok:
                continue
            candidates.append(node)

        if not candidates:
            return None
        # Idle nodes first, then the beefiest — a crude but effective placement.
        candidates.sort(key=lambda n: (n.current_task_id is not None, -n.ram_mb, n.name))
        return candidates[0]

    # -- dispatch --------------------------------------------------------

    async def dispatch(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        capability: NodeCapability,
        untrusted: bool = False,
        node_id: str | None = None,
        timeout: float = DEFAULT_DISPATCH_TIMEOUT,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a tool on a node and wait for the result."""
        if node_id is not None:
            node = store.get(node_id)
            if node is None or not self.is_connected(node_id):
                raise NoNodeAvailable(f"node {node_id} is not connected")
        else:
            node = self.select(capability, untrusted=untrusted)
            if node is None:
                detail = (
                    f"no connected node can run '{tool}' (needs {capability.value}"
                    f"{' and untrusted-execution consent' if untrusted else ''})"
                )
                raise NoNodeAvailable(detail)

        socket = self._sockets.get(node.node_id)
        if socket is None:
            raise NoNodeAvailable(f"node {node.node_id} disconnected before dispatch")

        self._counter += 1
        request_id = f"req_{self._counter}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        store.update_presence(node.node_id, current_task_id=task_id)
        try:
            await socket.send_text(json.dumps({
                "type": "dispatch",
                "request_id": request_id,
                "tool": tool,
                "arguments": arguments,
                "timeout": timeout,
            }))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise NoNodeAvailable(
                f"node {node.name} did not answer '{tool}' within {timeout:.0f}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            store.update_presence(node.node_id, clear_task=True)

    def resolve(self, request_id: str, payload: dict[str, Any]) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(payload)

    # -- inbound messages ------------------------------------------------

    async def handle_message(self, node_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        """Process one message from a node. Returns an optional reply."""
        kind = message.get("type")

        if kind == "heartbeat":
            metrics = message.get("metrics")
            store.update_presence(
                node_id,
                status=NodeStatus.ONLINE,
                metrics=metrics if isinstance(metrics, dict) else None,
            )
            return {"type": "heartbeat_ack"}

        if kind == "result":
            request_id = message.get("request_id", "")
            self.resolve(request_id, {
                "ok": bool(message.get("ok")),
                "output": message.get("output", ""),
                "error": message.get("error", ""),
            })
            return None

        if kind == "capabilities":
            store.update_presence(
                node_id,
                capabilities=parse_capabilities(message.get("capabilities")),
                ram_mb=message.get("ram_mb"),
                cores=message.get("cores"),
            )
            return {"type": "capabilities_ack"}

        return {"type": "error", "error": f"unknown message type: {kind!r}"}


gateway = NodeGateway()


def stale_cutoff_seconds() -> int:
    return HEARTBEAT_TIMEOUT_SECONDS
