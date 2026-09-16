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
    # Phase 1 uses a single shared passcode. Phase 2 replaces this with JWT
    # bearer tokens + WebAuthn passkeys per the design doc.
    passcode: str

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
        """No passcode set means the API is wide open.

        Fine for `uvicorn --reload` on localhost, dangerous on a public URL --
        `/health` reports this so the state is never a surprise.
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
        database_url=_env("DATABASE_URL"),
        sqlite_path=_env("JARVIS_DB_PATH", "jarvis_system.db"),
        app_name=_env("JARVIS_APP_NAME", "Jarvis"),
        debug=_env_bool("JARVIS_DEBUG", False),
    )
