"""Per-request correlation id and the mission it is currently serving.

The id lives in a contextvar so a log line, a mission row, and an engine call
can all see it without threading the value through every function. The
middleware sets it for the request. The scheduler copies it onto the mission
and re-binds it when the background task runs, because that task outlives the
request.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar, Token

_correlation: ContextVar[str] = ContextVar("jarvis_correlation_id", default="")
_mission: ContextVar[str] = ContextVar("jarvis_mission_id", default="")

# Reject anything that could break a log line or a response header: spaces,
# quotes, and newlines. Callers that send a bad value get a fresh id instead
# of their text echoed back.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

Scope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[MutableMapping[str, object]]]
Send = Callable[[MutableMapping[str, object]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def new_correlation_id() -> str:
    return f"cid_{uuid.uuid4().hex}"


def accept_correlation_id(raw: str) -> str:
    """Keep a caller-supplied id when it is safe. Otherwise mint one."""
    candidate = raw.strip()
    if _VALID_ID.fullmatch(candidate):
        return candidate
    return new_correlation_id()


def current_correlation_id() -> str:
    return _correlation.get()


def current_mission_id() -> str:
    return _mission.get()


@contextmanager
def bind_mission(mission_id: str, correlation_id: str | None = None) -> Iterator[None]:
    """Bind the mission for the enclosed block.

    `correlation_id=None` leaves the request's id in place. Passing a string,
    including an empty one, replaces it for the duration of the block so a
    resumed mission logs under the id that was stored with it.
    """
    mission_token = _mission.set(mission_id)
    correlation_token: Token[str] | None = None
    if correlation_id is not None:
        correlation_token = _correlation.set(correlation_id)
    try:
        yield
    finally:
        if correlation_token is not None:
            _correlation.reset(correlation_token)
        _mission.reset(mission_token)


class CorrelationMiddleware:
    """Assign or propagate `X-Correlation-ID` for HTTP requests.

    A pure ASGI middleware, not `BaseHTTPMiddleware`. The latter runs the
    endpoint in a child task, which drops contextvars on the way in, so the
    id set here would never reach the route.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-correlation-id")
        correlation_id = accept_correlation_id(incoming)
        token = _correlation.set(correlation_id)
        state = scope.get("state")
        if isinstance(state, dict):
            state["correlation_id"] = correlation_id

        async def send_with_id(message: MutableMapping[str, object]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(_response_headers(message))
                headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _correlation.reset(token)


def _header(scope: Scope, name: bytes) -> str:
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, list):
        return ""
    for item in raw_headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        key, value = item
        if isinstance(key, bytes) and key.lower() == name and isinstance(value, bytes):
            return value.decode("latin-1")
    return ""


def _response_headers(message: MutableMapping[str, object]) -> list[tuple[bytes, bytes]]:
    raw = message.get("headers")
    if not isinstance(raw, list):
        return []
    headers: list[tuple[bytes, bytes]] = []
    for item in raw:
        if isinstance(item, tuple) and len(item) == 2:
            key, value = item
            if isinstance(key, bytes) and isinstance(value, bytes):
                headers.append((key, value))
    return headers
