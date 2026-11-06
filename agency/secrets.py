"""Reject values that look like passwords or tokens.

The access checklist stores a status and a note. A brand profile stores
positioning and sample copy. Neither table has a column for a password, a
token, or an API key. This check is the second line: a secret pasted into a
note or a sample post is refused, and the error names the field without
repeating the value.
"""

from __future__ import annotations

import re
from typing import Any

from agency.models import AgencyError

# Assignment form ("password=...", "api_key: ...") plus well-known token shapes.
# The bare word "password" is ordinary copy and is allowed.
_SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key|auth[_-]?token|client[_-]?secret|authorization)"
    r"\s*[:=]\s*\S"
    r"|bearer\s+[a-z0-9\-._~+/]{8,}"
    r"|(?:sk|rk)[-_][a-z0-9_-]{12,}"
    r"|ghp_[a-z0-9]{8,}"
    r"|github_pat_[a-z0-9_]{8,}"
    r"|xox[baprs]-[a-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ya29\.[a-z0-9_\-]{8,}"
    r"|eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)

_SECRET_KEY = re.compile(
    r"(?i)^(password|passwd|secret|token|api_key|apikey|api-key|access_token|"
    r"refresh_token|private_key|client_secret|credential|credentials|authorization)$"
)


def reject_secrets(value: Any, field: str) -> None:
    """Raise when ``value`` carries a secret-shaped key or text.

    The message does not include the rejected text.
    """
    _walk(value, field)


def _walk(value: Any, field: str) -> None:
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            raise AgencyError(f"{field} looks like a password or token and was not stored.")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY.fullmatch(str(key)):
                raise AgencyError(f"{field} looks like a password or token and was not stored.")
            _walk(item, field)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, field)
