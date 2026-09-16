"""Shared test fixtures.

Every test runs with no provider keys set, so the suite exercises the real code
path end to end (including the echo engine) without touching the network or
costing a token.

By default the backing store is a throwaway SQLite file. Set
JARVIS_TEST_DATABASE_URL to a Postgres connection string to run the identical
suite against Postgres instead:

    JARVIS_TEST_DATABASE_URL=postgresql://... pytest -q

That matters because production is meant to run on Postgres (Neon) while local
development runs on SQLite — the dual-dialect storage layer is only trustworthy
if both are actually tested.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEST_PASSCODE = "test-passcode"

TEST_DATABASE_URL = os.getenv("JARVIS_TEST_DATABASE_URL", "").strip()

# Dropped and recreated between tests so each one starts empty.
_TABLES = (
    "tool_executions", "mood_checkins", "agent_trajectories", "sessions",
    "agent_messages", "approvals", "tasks", "missions", "memory_entries",
    "nodes", "node_enrolments", "todos", "todo_tracks",
)


def _reset_postgres() -> None:
    from app.core.db import execute, get_conn

    with get_conn() as conn:
        for table in _TABLES:
            execute(conn, f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolated settings + database for one test."""
    monkeypatch.setenv("JARVIS_PASSCODE", TEST_PASSCODE)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    if TEST_DATABASE_URL:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    else:
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.delenv("DATABASE_URL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()
    if TEST_DATABASE_URL:
        _reset_postgres()

    yield
    get_settings.cache_clear()


class ScriptedEngine:
    """Test double that returns a fixed sequence of engine results.

    The echo engine deliberately never emits tool calls, so it cannot exercise
    the agent loop. This lets tests drive tool calling, permission denial, and
    approval parking deterministically with no key and no network.
    """

    provider = "SCRIPTED"
    model = "scripted-test"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, max_tokens=1200):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.script:
            from app.agent.engines import EngineResult

            return EngineResult(text="done", provider=self.provider, model=self.model)
        return self.script.pop(0)

    def complete(self, system_prompt: str, user_prompt: str):  # noqa: ANN001
        return self.chat(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        )


def scripted_router(script: list[Any]):
    """An EngineRouter whose whole chain is one ScriptedEngine."""
    from app.agent.engines import EngineRouter

    router = EngineRouter()
    engine = ScriptedEngine(script)
    router.groq = None
    router.gemini = None
    router.echo = engine
    return router, engine


def text_result(text: str, **kwargs: Any):
    from app.agent.engines import EngineResult

    return EngineResult(
        text=text, provider="SCRIPTED", model="scripted-test",
        prompt_tokens=kwargs.pop("prompt_tokens", 10),
        completion_tokens=kwargs.pop("completion_tokens", 5), **kwargs,
    )


def tool_result(name: str, arguments: dict[str, Any], call_id: str = "call_1"):
    from app.agent.engines import EngineResult, ToolCall

    return EngineResult(
        text="", provider="SCRIPTED", model="scripted-test",
        prompt_tokens=10, completion_tokens=5,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
    )


@pytest.fixture()
def agents(env: None):
    """Loaded agent + tool registries against the temp database."""
    import app.tools.cloud  # noqa: F401 - registers tools
    import app.tools.node   # noqa: F401 - registers tools
    from app.agents.registry import registry as agent_registry
    from app.core.telemetry import telemetry as telemetry_engine
    from app.missions import store
    from app.memory import store as memory_store

    telemetry_engine.init_db()
    store.init_db()
    memory_store.init_db()
    agent_registry.load()
    return agent_registry


@pytest.fixture()
def telemetry(env: None):
    """A TelemetryEngine backed by the temp database."""
    from app.core.telemetry import telemetry as engine

    engine.init_db()
    return engine


@pytest.fixture()
def client(env: None):
    """TestClient with the passcode header pre-attached.

    The orchestrator binds an EngineRouter at import time, so the module is
    reloaded here to pick up this test's environment.
    """
    from fastapi.testclient import TestClient

    import app.agent.orchestrator as orchestrator_module
    import app.main as main_module

    importlib.reload(orchestrator_module)
    importlib.reload(main_module)

    with TestClient(main_module.app) as test_client:
        test_client.headers.update({"X-Jarvis-Passcode": TEST_PASSCODE})
        yield test_client


@pytest.fixture()
def session_id(client) -> str:
    response = client.post("/api/v1/session/start", json={})
    assert response.status_code == 201
    return response.json()["session_id"]
