"""HTTP surface limits: CORS origins, security headers, body size, rate limits.

The PWA is served from this process, so it never needs a cross-origin grant.
Anything else has to be named in JARVIS_CORS_ORIGINS. A `*` is ignored: pairing
it with Access-Control-Allow-Credentials would let any website call the API
with the operator's cookies.

There is no upload route. Bodies that claim to be a file are rejected, and
every body is capped, so a client cannot pin the worker with a huge payload.

Mission starts and model calls share one process-local window each. This is a
single-user system; a per-IP bucket would let a client rotate addresses around
the limit. A restart clears the windows. That only helps someone who can
already restart the process.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import traceback
from typing import Any

from fastapi import HTTPException, status
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

log = logging.getLogger("jarvis")

# The shell is same-origin: one stylesheet, one script, the manifest, and a
# service worker. No inline script and no inline style attribute (the mission
# progress width is set through the CSSOM, which this policy allows).
# frame-ancestors 'none' is what stops another site from embedding the UI.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

# File-shaped bodies. The API speaks JSON only; there is nowhere to store an
# upload, so these types are refused rather than parsed.
_UPLOAD_PREFIXES = ("multipart/", "image/", "audio/", "video/")
_UPLOAD_TYPES = frozenset({
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/x-zip-compressed",
    "application/gzip",
    "application/x-gzip",
})

_CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS = ["Authorization", "Content-Type", "X-Jarvis-Passcode"]

_lock = threading.Lock()
_hits: dict[str, list[float]] = {"mission": [], "llm": []}


def _now() -> float:
    """Wall clock, patched in tests so a rate-limit window does not sleep."""
    return time.time()


def reset_rate_limits() -> None:
    with _lock:
        for kind in _hits:
            _hits[kind] = []


def is_disallowed_upload(content_type: str | None) -> bool:
    """True when the body claims to be a file. This API has no upload route."""
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    if media in _UPLOAD_TYPES:
        return True
    return any(media.startswith(prefix) for prefix in _UPLOAD_PREFIXES)


def cors_middleware_config() -> dict[str, Any]:
    """Arguments for CORSMiddleware. Never a wildcard, never with credentials.

    Credentials are on only when at least one explicit origin is configured.
    An empty allowlist leaves the PWA on same-origin and grants nothing else.
    """
    settings = get_settings()
    origins = list(settings.cors_origins)
    return {
        "allow_origins": origins,
        "allow_credentials": bool(origins),
        "allow_methods": list(_CORS_METHODS),
        "allow_headers": list(_CORS_HEADERS),
    }


def consume_rate_limit(kind: str) -> tuple[bool, int]:
    """Record one hit. Returns (allowed, retry_after_seconds)."""
    settings = get_settings()
    if kind == "mission":
        limit = settings.mission_rate_limit
    elif kind == "llm":
        limit = settings.llm_rate_limit
    else:
        raise ValueError(f"unknown rate limit {kind}")
    window = settings.rate_window_seconds
    now = _now()
    with _lock:
        recent = [stamp for stamp in _hits[kind] if stamp > now - window]
        if len(recent) >= limit:
            _hits[kind] = recent
            retry = max(1, math.ceil(recent[0] + window - now))
            return False, retry
        recent.append(now)
        _hits[kind] = recent
        return True, 0


def enforce_rate_limit(kind: str) -> None:
    """Raise 429 when this class of expensive call is over its window."""
    allowed, retry = consume_rate_limit(kind)
    if allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Try again later.",
        headers={"Retry-After": str(retry)},
    )


def client_failure_detail(summary: str, exc: BaseException) -> str:
    """What a caught failure may say to the client.

    The exception text can carry a DSN, a provider key, or a stack fragment
    from a library. Outside debug mode the client gets the summary only.
    The full exception is logged by the caller.
    """
    if get_settings().debug:
        return f"{summary}: {exc}"
    return f"{summary}."


def server_error_body(exc: BaseException) -> dict[str, Any]:
    """JSON for an unhandled error. No traceback and no config unless debug."""
    body: dict[str, Any] = {"detail": "Internal server error."}
    if get_settings().debug:
        body["error"] = f"{type(exc).__name__}: {exc}"
        body["trace"] = traceback.format_exc()
    return body


def apply_security_headers(response: JSONResponse) -> JSONResponse:
    """Stamp the browser policy onto a response we build ourselves.

    Unhandled exceptions are rendered by the server-error middleware, which
    sits outside the header middleware and would otherwise send a bare 500.
    """
    response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-frame-options"] = "DENY"
    return response


def server_error_response(exc: BaseException) -> JSONResponse:
    return apply_security_headers(JSONResponse(status_code=500, content=server_error_body(exc)))


class SecurityHeadersMiddleware:
    """Attach the browser policy to every HTTP response, including errors.

    Raw ASGI, not BaseHTTPMiddleware: the dataset export is a streaming
    response, and BaseHTTPMiddleware buffers that stream.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers.setdefault("content-security-policy", CONTENT_SECURITY_POLICY)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "no-referrer")
                # Older clients ignore frame-ancestors. DENY is the same rule.
                headers.setdefault("x-frame-options", "DENY")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestGuardMiddleware:
    """Reject file uploads and bodies over the configured cap.

    Content-Length is checked before the body is read, so a declared giant
    payload is refused without buffering it. A missing or lying length is
    still counted, up to the cap, and then refused.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if is_disallowed_upload(headers.get("content-type")):
            await _send_json(send, 415, "This server does not accept file uploads.")
            return

        max_bytes = get_settings().max_body_bytes
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError:
                await _send_json(send, 400, "Invalid Content-Length.")
                return
            if declared < 0 or declared > max_bytes:
                await _send_json(send, 413, "Request body is too large.")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect before the body is finished. Hand it on as-is
                # only when we have not already decided to refuse.
                if body:
                    await self.app(scope, _replay(bytes(body), receive), send)
                else:
                    await self.app(scope, _one_then(message, receive), send)
                return
            chunk = message.get("body") or b""
            if len(body) + len(chunk) > max_bytes:
                await _send_json(send, 413, "Request body is too large.")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        await self.app(scope, _replay(bytes(body), receive), send)


def _replay(body: bytes, receive: Receive) -> Receive:
    pending: list[Message | None] = [
        {"type": "http.request", "body": body, "more_body": False}
    ]

    async def replay() -> Message:
        if pending:
            message = pending.pop(0)
            if message is not None:
                return message
        return await receive()

    return replay


def _one_then(first: Message, receive: Receive) -> Receive:
    pending: list[Message | None] = [first]

    async def inner() -> Message:
        if pending:
            message = pending.pop(0)
            if message is not None:
                return message
        return await receive()

    return inner


async def _send_json(send: Send, status_code: int, detail: str) -> None:
    payload = json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})
