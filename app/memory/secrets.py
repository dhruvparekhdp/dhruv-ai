"""Detect credential-shaped text so memory can refuse to store it.

This mirrors the log redaction patterns: bearer tokens, labeled passwords,
keys, and one-time codes. Memory does not keep a redacted stand-in. A stored
"[REDACTED]" would itself become a fact the assistant might repeat, so callers
reject the write instead.

The match result is the kind of credential (a label), never the secret.
"""

from __future__ import annotations

import re

# Bearer first. `Authorization: Bearer <secret>` would otherwise be parsed as
# the single word "Bearer", and the secret would be missed.
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{6,}")

_LABELED = re.compile(
    r"""(?ix)
    ["']?
    \b(?P<label>
        authorization
        | password
        | passwd
        | pwd
        | secret
        | token
        | api[_-]?key
        | access[_-]?key
        | refresh[_-]?token
        | private[_-]?key
        | totp(?:[_-]?(?:secret|seed|key|code))?
        | otp
        | 2fa
    )\b
    ["']?
    \s*[:=]\s*
    ["']?
    [^\s"',}]+
    """
)

# A key that is itself a credential name. "password" / "hunter2" has no
# `password=` label inside the value, and it is still a secret.
_CREDENTIAL_KEYS = frozenset({
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "refresh_token",
    "private_key",
    "totp",
    "totp_secret",
    "otp",
    "authorization",
    "2fa",
})


def secret_label(*parts: str) -> str | None:
    """Return the credential kind found in `parts`, or None.

    The return value is a label such as `password` or `authorization`. It is
    safe to put in an error message. The secret text is not returned.
    """
    for part in parts:
        label = _label_in(part)
        if label:
            return label
    return None


def _label_in(text: str) -> str | None:
    if not text:
        return None
    if _BEARER.search(text):
        return "authorization"
    match = _LABELED.search(text)
    if match:
        return _normalise_label(match.group("label"))
    # The whole string is a credential name, as a memory key would be.
    normalised = _normalise_label(text)
    if normalised in _CREDENTIAL_KEYS:
        return normalised
    return None


def _normalise_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().strip("'\"").lower()).strip("_")
