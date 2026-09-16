"""Access control for the Phase 1 public deployment.

The design doc specifies JWT bearer tokens plus WebAuthn passkeys, which is the
Phase 2 target. That is too much to stand up in the first slice -- but shipping
a personal mood journal on a public URL with *no* gate at all is worse: anyone
who finds the hostname could read your entries and burn your free API quota.

So Phase 1 uses a single shared passcode from the environment, compared in
constant time, required on every /api/v1 route and on the WebSocket handshake.
Small, and it closes the hole until passkeys land.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import get_settings

PASSCODE_HEADER = "X-Jarvis-Passcode"


def check_passcode(candidate: str | None) -> bool:
    """Constant-time passcode comparison.

    Returns True when auth is disabled (no passcode configured), which is the
    local-development case. /health advertises that state so an unprotected
    public deploy is visible rather than silent.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, settings.passcode)


async def require_passcode(
    x_jarvis_passcode: str | None = Header(default=None, alias=PASSCODE_HEADER),
) -> None:
    """FastAPI dependency guarding the REST API."""
    if not check_passcode(x_jarvis_passcode):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing passcode.",
        )
