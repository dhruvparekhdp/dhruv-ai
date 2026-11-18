"""Bearer check for trading-v1 servers.

The engine compares the presented token with INTEGRATION_API_TOKEN using
hmac.compare_digest. An empty expected token fails closed: a missing secret
must not leave the feed open. This module never logs either value.
"""

from __future__ import annotations

import hmac


def parse_bearer(header: str | None) -> str | None:
    """Return the token from an Authorization header, or None if it is not Bearer."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer":
        return None
    token = token.strip()
    if not token or " " in token:
        return None
    return token


def bearer_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time match. Missing or empty expected token never matches."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)
