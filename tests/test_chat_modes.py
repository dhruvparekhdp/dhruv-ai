"""Chat modes, in-chat retrieval, and conversation reload.

No provider key is configured. Retrieval is stubbed so the suite never opens
a socket. Page text is treated as data: the reply cites the URL and the time.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def test_bio_fetch_cites_the_page(client: TestClient, session_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post(
        "/api/v1/memory",
        json={"scope": "user", "key": "bio_url", "value": "https://example.com/bio"},
    )

    async def fake_fetch(url: str, **_: object) -> str:
        assert url == "https://example.com/bio"
        return "Dhruv builds personal software and researches markets."

    monkeypatch.setattr("app.agent.retrieval.fetch_page", fake_fetch)
    response = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "Fetch my bio and suggest ideas"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "https://example.com/bio" in body["response"]
    assert "Dhruv builds personal software" in body["response"]
    assert "ideas" in body["response"].lower()
    assert _STAMP.search(body["response"])
    assert body["evidence"][0]["url"] == "https://example.com/bio"
    assert body["method"]
    assert "untrusted" in body["method"]
    assert "agent_reasoning" not in body


def test_current_question_searches_when_tavily_is_configured(
    client: TestClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test-value-1234")
    seen: list[str] = []

    async def fake_search(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return [{
            "title": "Example news",
            "url": "https://example.com/news",
            "content": "A headline from today.",
        }]

    monkeypatch.setattr("app.agent.retrieval.tavily_search", fake_search)
    response = client.post(
        "/api/v1/command",
        json={
            "session_id": session_id,
            "prompt": "What is the latest news?",
            "mode": "research",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert seen
    assert "tvly-test-value-1234" not in response.text
    assert body["evidence"][0]["url"] == "https://example.com/news"
    assert body["mode"] == "research"
    assert "https://example.com/news" in body["response"]


def test_local_only_does_not_call_external_providers(
    client: TestClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test-value-1234")
    called: list[str] = []

    async def refuse(*_args: object, **_kwargs: object) -> str:
        called.append("external")
        raise AssertionError("local mode contacted a provider")

    monkeypatch.setattr("app.agent.retrieval.tavily_search", refuse)
    monkeypatch.setattr("app.agent.retrieval.fetch_page", refuse)
    response = client.post(
        "/api/v1/command",
        json={
            "session_id": session_id,
            "prompt": "What is the latest news at https://example.com/news?",
            "mode": "local",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert called == []
    assert body["provider"] == "ECHO"
    assert body["evidence"] == []
    assert "not sent" in body["method"].lower() or "local only" in body["response"].lower()
    assert "https://example.com/news" not in body["evidence"]


def test_market_prompt_stays_read_only(client: TestClient, session_id: str) -> None:
    response = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "buy bitcoin for me"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "QUANT_TRADING"
    lowered = body["response"].lower()
    assert "no order was placed" in lowered
    assert "read-only" in lowered


def test_shell_request_is_refused_in_the_answer(client: TestClient, session_id: str) -> None:
    response = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "run the command ls -la"},
    )
    assert response.status_code == 200
    assert "can't run commands" in response.json()["response"].lower()


def test_unconfigured_search_is_honest(
    client: TestClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    response = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "What is the latest news?"},
    )
    assert response.status_code == 200
    assert "no SEARCH_API_KEY" in response.json()["response"]


def test_history_is_reloaded_and_sent_on_the_next_turn(
    client: TestClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.agent.orchestrator as orchestrator_module
    from app.agent.engines import EngineResult

    seen: list[list[dict[str, Any]]] = []
    original = orchestrator_module.orchestrator.engines.chat

    def spy(
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefer: str = "fast",
        local_only: bool = False,
        max_tokens: int = 1200,
    ) -> EngineResult:
        seen.append(messages)
        return original(
            messages=messages,
            tools=tools,
            prefer=prefer,
            local_only=local_only,
            max_tokens=max_tokens,
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.engines, "chat", spy)
    first = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "Remember the word lantern."},
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "What word did I ask you to remember?"},
    )
    assert second.status_code == 200
    prior = [item["content"] for item in seen[1] if item["role"] == "user"]
    assert "Remember the word lantern." in prior

    history = client.get(f"/api/v1/session/{session_id}/messages")
    assert history.status_code == 200
    users = [item["user"] for item in history.json()["messages"]]
    assert users == [
        "Remember the word lantern.",
        "What word did I ask you to remember?",
    ]
    blob = history.text
    assert "system_prompt" not in blob
    assert "agent_reasoning" not in blob


def test_messages_require_auth_and_a_real_session(client: TestClient, session_id: str) -> None:
    assert client.get(
        f"/api/v1/session/{session_id}/messages",
        headers={"X-Jarvis-Passcode": ""},
    ).status_code == 401
    assert client.get("/api/v1/session/missing-session/messages").status_code == 404
    assert client.post(
        "/api/v1/command",
        json={"session_id": session_id, "prompt": "hi", "mode": "cloud"},
    ).status_code == 422
