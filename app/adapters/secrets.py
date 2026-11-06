"""Where adapter credentials come from.

The default source is the process environment. A host secret manager can be
injected behind the same `get(name)` shape. Values are returned to the caller
and must not be logged — this module never logs.
"""

from __future__ import annotations

import os
from typing import Protocol


class SecretSource(Protocol):
    def get(self, name: str) -> str:
        """Return the secret for `name`, or "" when it is not configured."""


class EnvSecretSource:
    """Read a secret from the environment at call time.

    Not cached: rotating a credential in the environment takes effect on the
    next request without a process restart. The name is the only thing a log
    line may mention.
    """

    def get(self, name: str) -> str:
        if not name:
            return ""
        return os.environ.get(name, "").strip()
