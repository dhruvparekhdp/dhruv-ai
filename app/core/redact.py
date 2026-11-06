"""Strip secrets out of log text.

The filter is deliberately pattern-based and conservative: it redacts values
that are labeled as credentials, bearer tokens, and one-time codes. It does
not try to guess that a bare number is a TOTP, because task counts and ports
are numbers too.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Bearer first. An `Authorization: Bearer <secret>` line would otherwise have
# its value parsed as the single word "Bearer", leaving the secret in place.
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{6,}")

_LABELED = re.compile(
    r"""(?ix)
    (?P<label>
        ["']?
        \b(?:
            authorization
            | password
            | passwd
            | pwd
            | secret
            | token
            | api[_-]?key
            | access[_-]?key
            | refresh[_-]?token
            | totp(?:[_-]?(?:secret|seed|key|code))?
            | otp
            | 2fa
        )
        \b
        ["']?
    )
    (?P<sep>\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^\s"',}]+)
    """
)


def redact(text: str) -> str:
    """Return `text` with credential values replaced by `[REDACTED]`."""
    without_bearer = _BEARER.sub(f"Bearer {REDACTED}", text)

    def _replace(match: re.Match[str]) -> str:
        return f"{match.group('label')}{match.group('sep')}{match.group('quote')}{REDACTED}"

    return _LABELED.sub(_replace, without_bearer)
