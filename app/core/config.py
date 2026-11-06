"""Environment-driven configuration for the Jarvis host engine.

Everything Jarvis needs to boot comes from environment variables so the same
image runs locally, on Render, or on any other host without code changes.
Nothing here reads a secret into a prompt -- keys are handed only to the
specific engine client that needs them (Security Protocol #1, credential vault
separation).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env(key: str, default: str = "") -> str:
    """Read an env var, trimming whitespace that sneaks in via dashboard UIs."""
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    """Parse an integer setting, falling back to the default when it is unusable.

    A typo must not silently disable expiry or lockout, so values below
    `minimum` (or above `maximum`) are rejected in favour of the default.
    """
    raw = _env(key)
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings snapshot."""

    # --- LLM engines -----------------------------------------------------
    groq_api_key: str
    gemini_api_key: str

    # NOTE: the design doc specified `llama-3.3-70b-versatile`, but Groq is
    # shutting that model down on 2026-08-16. `openai/gpt-oss-120b` is Groq's
    # own recommended replacement for it. Override via GROQ_MODEL if needed.
    groq_model: str
    gemini_model: str

    # Optional Tavily key for web.search. Absent, the tool reports itself as
    # unconfigured rather than inventing results.
    search_api_key: str

    # --- Access control --------------------------------------------------
    # Phase 1 uses a single shared passcode, exchanged for an expiring session
    # token. Phase 2 replaces this with JWT bearer tokens + WebAuthn passkeys
    # per the design doc. An unset passcode refuses to boot (D-026); the only
    # exception is an explicit loopback dev mode.
    passcode: str
    dev_insecure: bool
    host: str
    port: int
    allow_public_bind: bool
    session_ttl_seconds: int
    login_max_failures: int
    login_backoff_seconds: int
    login_backoff_max_seconds: int

    # --- Storage ---------------------------------------------------------
    # Empty  -> local SQLite file (ephemeral on Render's free tier).
    # postgres://... -> Neon/any Postgres, and telemetry survives redeploys.
    database_url: str
    sqlite_path: str

    # --- Misc ------------------------------------------------------------
    app_name: str
    debug: bool

    @property
    def groq_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def auth_enabled(self) -> bool:
        """True when a passcode is configured.

        False only in the loopback dev exception (JARVIS_DEV_INSECURE=1).
        `/health` reports this so the state is never a surprise. Startup
        refuses to reach this property's "off" state on any other bind.
        """
        return bool(self.passcode)

    @property
    def storage_backend(self) -> str:
        return "postgres" if self.database_url.startswith("postgres") else "sqlite"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) settings from the environment."""
    return Settings(
        groq_api_key=_env("GROQ_API_KEY"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        groq_model=_env("GROQ_MODEL", "openai/gpt-oss-120b"),
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        search_api_key=_env("SEARCH_API_KEY"),
        passcode=_env("JARVIS_PASSCODE"),
        dev_insecure=_env_bool("JARVIS_DEV_INSECURE", False),
        # Loopback unless the operator opts into a public bind. Empty is the
        # same as unset: an accidental blank must not become "listen everywhere".
        host=_env("JARVIS_HOST", "127.0.0.1") or "127.0.0.1",
        port=_env_int("JARVIS_PORT", 8000, minimum=1, maximum=65535),
        allow_public_bind=_env_bool("JARVIS_ALLOW_PUBLIC_BIND", False),
        session_ttl_seconds=_env_int(
            "JARVIS_SESSION_TTL_SECONDS", 12 * 60 * 60, minimum=1
        ),
        login_max_failures=_env_int("JARVIS_LOGIN_MAX_FAILURES", 5, minimum=1),
        login_backoff_seconds=_env_int("JARVIS_LOGIN_BACKOFF_SECONDS", 30, minimum=1),
        login_backoff_max_seconds=_env_int(
            "JARVIS_LOGIN_BACKOFF_MAX_SECONDS", 15 * 60, minimum=1
        ),
        database_url=_env("DATABASE_URL"),
        sqlite_path=_env("JARVIS_DB_PATH", "jarvis_system.db"),
        app_name=_env("JARVIS_APP_NAME", "Jarvis"),
        debug=_env_bool("JARVIS_DEBUG", False),
    )
