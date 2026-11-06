"""Access control for the Jarvis host.

A single shared passcode (D-004) is the credential. It is compared in constant
time and exchanged for a session token that expires. Login failures share one
global counter: this is a single-user system, so a per-IP bucket would let an
attacker rotate addresses around the lockout.

Startup refuses to serve when that passcode is missing. The only exception is
an explicit dev switch AND a loopback bind, which logs a loud warning (D-026).
Binding beyond loopback requires its own explicit flag, so `0.0.0.0` cannot
happen because a process manager's default host was copied from a tutorial.

Sessions and the lockout live in memory. A crash drops both, which fails
closed for sessions (the operator logs in again) and is only reachable for
the lockout by someone who can already restart the process.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Header, HTTPException, status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings, get_settings

log = logging.getLogger("jarvis")

PASSCODE_HEADER = "X-Jarvis-Passcode"
TOKEN_HEADER = "X-Jarvis-Token"

# Long enough for a real passphrase, short enough that a multi-megabyte header
# cannot pin the worker inside sha256. Longer candidates cannot match.
_MAX_PASSCODE_CHARS = 500

# One person, a handful of browsers. Cap the table so a login loop cannot
# grow it without bound; the oldest session is dropped first.
_MAX_SESSIONS = 32

_PUBLIC_PATHS = frozenset({
    "/health",
    "/",
    "/favicon.ico",
    "/manifest.json",
    "/sw.js",
})

_LOGIN_PATH = "/api/v1/auth/login"


class StartupRefused(RuntimeError):
    """The process must not accept traffic. Raised from lifespan and the launcher."""


@dataclass
class _Session:
    token_hash: str
    created_at: float
    expires_at: float


@dataclass
class _LoginGuard:
    failures: int = 0
    locked_until: float = 0.0


_lock = threading.Lock()
_sessions: dict[str, _Session] = {}
_guard = _LoginGuard()
_insecure_warning_logged = False


def _now() -> float:
    """Wall clock, patched in tests so expiry and backoff do not sleep."""
    return time.time()


def reset_auth_state() -> None:
    """Drop sessions and the lockout. Tests call this between cases."""
    global _insecure_warning_logged
    with _lock:
        _sessions.clear()
        _guard.failures = 0
        _guard.locked_until = 0.0
        _insecure_warning_logged = False


def is_loopback(host: str) -> bool:
    """True for localhost and the loopback ranges. `0.0.0.0` and `::` are not."""
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _invoked_via_uvicorn(argv: list[str]) -> bool:
    if not argv:
        return False
    prog = Path(argv[0]).name.lower()
    if prog.startswith("uvicorn"):
        return True
    # `python -m uvicorn` keeps argv[0] as python and names uvicorn next.
    if len(argv) >= 3 and argv[1] == "-m" and argv[2].split(".")[0] == "uvicorn":
        return True
    # Some installers re-exec as uvicorn's __main__.py. `python -m app` also
    # lands on an __main__.py, so require the uvicorn package to be in the path.
    if prog == "__main__.py":
        parts = argv[0].replace("\\", "/").split("/")
        return "uvicorn" in parts
    return False


def host_from_argv(argv: list[str] | None = None) -> str | None:
    """The `--host` uvicorn was actually given, if this process is uvicorn.

    `JARVIS_HOST` is not enough on its own: `uvicorn --host 0.0.0.0` binds
    that address regardless of the setting. The flag is only meaningful when
    uvicorn is the entry point, so pytest's own argv cannot trip it.
    """
    args = list(sys.argv if argv is None else argv)
    if not _invoked_via_uvicorn(args):
        return None
    for index, arg in enumerate(args):
        if arg == "--host" and index + 1 < len(args):
            return args[index + 1].strip()
        if arg.startswith("--host="):
            return arg.split("=", 1)[1].strip()
    return None


def effective_bind_host(settings: Settings | None = None, argv: list[str] | None = None) -> str:
    """Address the process will listen on. The uvicorn flag wins when present."""
    cli_host = host_from_argv(argv)
    if cli_host:
        return cli_host
    current = settings if settings is not None else get_settings()
    return current.host


def enforce_startup_policy(settings: Settings | None = None, *, bind_host: str | None = None) -> str:
    """Refuse to serve an open or accidentally public process.

    Returns the bind host the check used. Raises StartupRefused otherwise.
    """
    current = settings if settings is not None else get_settings()
    host = bind_host if bind_host is not None else effective_bind_host(current)
    loopback = is_loopback(host)

    if not current.passcode:
        if current.dev_insecure and loopback:
            _warn_dev_insecure(host)
            return host
        extra = ""
        if not loopback:
            extra = " JARVIS_DEV_INSECURE is only honoured on a loopback bind."
        elif not current.dev_insecure:
            extra = (
                " For loopback-only development set JARVIS_DEV_INSECURE=1"
                " and bind 127.0.0.1."
            )
        raise StartupRefused("Refusing to start: JARVIS_PASSCODE is unset." + extra)

    if not loopback and not current.allow_public_bind:
        raise StartupRefused(
            f"Refusing to bind to {host}: set JARVIS_ALLOW_PUBLIC_BIND=1"
            " to listen beyond loopback."
        )
    return host


def _warn_dev_insecure(host: str) -> None:
    global _insecure_warning_logged
    with _lock:
        if _insecure_warning_logged:
            return
        _insecure_warning_logged = True
    log.warning(
        "\n****************************************************************\n"
        "JARVIS_DEV_INSECURE=1 — authentication is DISABLED.\n"
        "Bound to %s only. Do not publish this process.\n"
        "****************************************************************",
        host,
    )


def _passcode_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def passcode_matches(candidate: str | None) -> bool:
    """Constant-time passcode check.

    sha256 first so a different length cannot return early inside
    compare_digest. When auth is disabled (the loopback dev exception) every
    candidate matches: there is no secret to check.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    given = candidate or ""
    if len(given) > _MAX_PASSCODE_CHARS:
        given = ""
    return hmac.compare_digest(_passcode_digest(settings.passcode), _passcode_digest(given))


def check_passcode(candidate: str | None) -> bool:
    """Compatibility wrapper. Prefer authorize() for request gates."""
    return passcode_matches(candidate)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune_sessions(now: float) -> None:
    expired = [key for key, session in _sessions.items() if session.expires_at <= now]
    for key in expired:
        _sessions.pop(key, None)
    overflow = len(_sessions) - _MAX_SESSIONS
    if overflow <= 0:
        return
    oldest = sorted(_sessions.values(), key=lambda session: session.created_at)
    for session in oldest[:overflow]:
        _sessions.pop(session.token_hash, None)


def issue_token() -> tuple[str, float]:
    """Mint a session token. Returns (raw token, unix expiry). The raw value is not stored."""
    now = _now()
    ttl = get_settings().session_ttl_seconds
    token = secrets.token_urlsafe(32)
    expires_at = now + ttl
    digest = _token_hash(token)
    with _lock:
        _prune_sessions(now)
        _sessions[digest] = _Session(token_hash=digest, created_at=now, expires_at=expires_at)
    return token, expires_at


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    current = _now() if now is None else now
    digest = _token_hash(token)
    with _lock:
        session = _sessions.get(digest)
        if session is None:
            return False
        if session.expires_at <= current:
            _sessions.pop(digest, None)
            return False
        return True


def revoke_token(token: str | None) -> bool:
    if not token:
        return False
    digest = _token_hash(token)
    with _lock:
        return _sessions.pop(digest, None) is not None


def _locked(now: float) -> bool:
    return _guard.locked_until > now


def retry_after_seconds(now: float | None = None) -> int:
    current = _now() if now is None else now
    with _lock:
        remaining = _guard.locked_until - current
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining))


def attempt_login(candidate: str | None) -> str:
    """Check a passcode and update the lockout.

    Returns "ok", "bad", or "locked". A correct passcode during the lock
    window stays locked, so the window cannot be probed. Success clears it.
    """
    now = _now()
    matches = passcode_matches(candidate)
    settings = get_settings()
    with _lock:
        if _locked(now):
            return "locked"
        if not matches:
            _guard.failures += 1
            if _guard.failures >= settings.login_max_failures:
                exponent = _guard.failures - settings.login_max_failures
                # Cap the shift so a huge failure count cannot overflow the delay.
                delay = settings.login_backoff_seconds * (2 ** min(exponent, 16))
                delay = min(delay, settings.login_backoff_max_seconds)
                _guard.locked_until = now + delay
                return "locked"
            return "bad"
        _guard.failures = 0
        _guard.locked_until = 0.0
        return "ok"


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None


def presented_token(x_jarvis_token: str | None, authorization: str | None) -> str | None:
    if x_jarvis_token and x_jarvis_token.strip():
        return x_jarvis_token.strip()
    return extract_bearer(authorization)


def authorize(token: str | None, passcode: str | None) -> str:
    """Decide a request. "ok", "locked", or "deny".

    A blank passcode is treated as absent so clients can clear the header
    without burning a login attempt. A presented passcode shares the login
    lockout, otherwise guessing via the header would skip it.
    """
    if not get_settings().auth_enabled:
        return "ok"
    if token_valid(token):
        return "ok"
    if passcode:
        return attempt_login(passcode)
    return "deny"


def auth_error(decision: str) -> HTTPException:
    if decision == "locked":
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after_seconds())},
        )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing passcode.",
    )


async def require_passcode(
    x_jarvis_passcode: str | None = Header(default=None, alias=PASSCODE_HEADER),
    x_jarvis_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency guarding routes that declare it.

    The ASGI gate below is what actually covers every route, including ones
    that forget this dependency. Both call authorize() so they cannot drift.
    A failed passcode is counted once: the gate returns before the dependency
    runs. A success resets the counter twice, which is a no-op the second time.
    """
    decision = authorize(presented_token(x_jarvis_token, authorization), x_jarvis_passcode)
    if decision != "ok":
        raise auth_error(decision)


def is_public_http(path: str, method: str) -> bool:
    """Routes that must answer without a session.

    The PWA shell is public so the lock screen can load. `/health` is public
    for uptime probes. Login is public because the passcode travels in the
    body. Everything else, including docs and unknown paths, stays gated.
    OPTIONS is left open so a cross-origin preflight can complete; the real
    request is still checked.
    """
    verb = method.upper()
    if verb == "OPTIONS":
        return True
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return True
    return verb == "POST" and path == _LOGIN_PATH


class AuthGateMiddleware:
    """Reject unauthenticated HTTP before routing.

    Written as raw ASGI rather than BaseHTTPMiddleware because the dataset
    export is a streaming response, and BaseHTTPMiddleware buffers or breaks
    that stream. WebSocket scopes are passed through; those handlers check
    credentials themselves (browsers cannot set headers on the handshake).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        method = scope.get("method") or "GET"
        if is_public_http(path, method):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        decision = authorize(
            presented_token(headers.get(TOKEN_HEADER.lower()), headers.get("authorization")),
            headers.get(PASSCODE_HEADER.lower()),
        )
        if decision == "ok":
            await self.app(scope, receive, send)
            return
        await _send_auth_response(send, decision)


async def _send_auth_response(send: Send, decision: str) -> None:
    if decision == "locked":
        status_code = 429
        detail = "Too many failed login attempts. Try again later."
        retry = str(retry_after_seconds())
    else:
        status_code = 401
        detail = "Invalid or missing passcode."
        retry = ""
    # A hand-built response keeps this middleware off BaseHTTPMiddleware, which
    # would buffer the dataset export stream on requests that do get through.
    body = json.dumps({"detail": detail}).encode("utf-8")
    header_list = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if retry:
        header_list.append((b"retry-after", retry.encode("ascii")))
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": header_list,
    })
    await send({"type": "http.response.body", "body": body})
