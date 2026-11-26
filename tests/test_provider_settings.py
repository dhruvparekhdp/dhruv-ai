"""Keys page: server-side provider settings.

Secrets stay out of list responses, health, readiness, the database row, and
the browser script. An environment value cannot be replaced from the UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.db import fetch_all, get_conn

SAMPLE_GROQ = "gsk_test_value_123456"


def _groq(body: dict[str, Any]) -> dict[str, Any]:
    providers = body["providers"]
    assert isinstance(providers, list)
    found = next(item for item in providers if item["id"] == "groq")
    assert isinstance(found, dict)
    return found


def _field(provider: dict[str, Any], name: str) -> dict[str, Any]:
    fields = provider["fields"]
    assert isinstance(fields, list)
    found = next(item for item in fields if item["name"] == name)
    assert isinstance(found, dict)
    return found


def test_status_lists_only_real_providers_and_hides_secrets(client: TestClient) -> None:
    saved = client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": SAMPLE_GROQ, "model": "demo-model"}},
    )
    assert saved.status_code == 200, saved.text
    assert SAMPLE_GROQ not in saved.text

    listed = client.get("/api/v1/settings/providers")
    assert listed.status_code == 200
    body = listed.json()
    assert [item["id"] for item in body["providers"]] == ["groq", "gemini", "tavily", "ollama"]
    groq = _groq(body)
    assert groq["configured"] is True
    key = _field(groq, "api_key")
    assert key["source"] == "saved"
    assert key["secret"] is True
    assert "value" not in key
    assert _field(groq, "model")["value"] == "demo-model"
    assert _field(groq, "model")["source"] == "saved"
    assert SAMPLE_GROQ not in listed.text

    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT stored_value FROM provider_settings WHERE field_name = 'api_key'")
    assert rows
    assert SAMPLE_GROQ not in rows[0]["stored_value"]
    assert str(rows[0]["stored_value"]).startswith("seal:")


def test_reveal_replace_and_clear_round_trip(client: TestClient) -> None:
    assert client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": SAMPLE_GROQ}},
    ).status_code == 200

    revealed = client.post("/api/v1/settings/providers/groq/reveal", json={"field": "api_key"})
    assert revealed.status_code == 200
    assert revealed.json() == {"field": "api_key", "value": SAMPLE_GROQ}

    replaced = client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": "gsk_replaced_secret"}},
    )
    assert replaced.status_code == 200
    assert client.post("/api/v1/settings/providers/groq/reveal", json={"field": "api_key"}).json()["value"] == (
        "gsk_replaced_secret"
    )

    cleared = client.delete("/api/v1/settings/providers/groq?field=api_key")
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["configured"] is False
    assert client.post("/api/v1/settings/providers/groq/reveal", json={"field": "api_key"}).json()["value"] == ""


def test_saved_key_changes_routing_without_restart(client: TestClient) -> None:
    before = client.get("/health").json()["engines"]
    assert before["degraded"] is True
    assert "groq" not in before["available"]

    assert client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": SAMPLE_GROQ}},
    ).status_code == 200

    health = client.get("/health")
    engines = health.json()["engines"]
    assert "groq" in engines["available"]
    assert engines["degraded"] is False
    assert SAMPLE_GROQ not in health.text

    ready = client.get("/ready")
    assert ready.json()["checks"]["integrations"]["groq"] == "configured"
    assert ready.json()["checks"]["integrations"]["ollama"] == "unconfigured"
    assert SAMPLE_GROQ not in ready.text

    client.delete("/api/v1/settings/providers/groq?field=api_key")
    after = client.get("/health").json()["engines"]
    assert after["degraded"] is True
    assert "groq" not in after["available"]


def test_environment_value_is_not_overridden(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "env-secret-value")
    monkeypatch.setenv("GROQ_MODEL", "env-model")

    denied = client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": SAMPLE_GROQ, "model": "ui-model"}},
    )
    assert denied.status_code == 409
    assert "env-secret-value" not in denied.text
    assert SAMPLE_GROQ not in denied.text

    listed = client.get("/api/v1/settings/providers")
    groq = _groq(listed.json())
    assert _field(groq, "api_key")["source"] == "environment"
    assert _field(groq, "api_key")["managed"] is True
    assert "value" not in _field(groq, "api_key")
    assert _field(groq, "model")["source"] == "environment"
    assert _field(groq, "model")["value"] == "env-model"
    assert "env-secret-value" not in listed.text

    cleared = client.delete("/api/v1/settings/providers/groq?field=api_key")
    assert cleared.status_code == 409

    revealed = client.post("/api/v1/settings/providers/groq/reveal", json={"field": "api_key"})
    assert revealed.json()["value"] == "env-secret-value"

    health = client.get("/health")
    assert "groq" in health.json()["engines"]["available"]
    assert health.json()["engines"]["groq_model"] == "env-model"
    assert "env-secret-value" not in health.text


def test_ollama_saved_enabled_is_a_real_engine(client: TestClient) -> None:
    saved = client.put(
        "/api/v1/settings/providers/ollama",
        json={"fields": {"enabled": "true"}},
    )
    assert saved.status_code == 200, saved.text
    engines = client.get("/health").json()["engines"]
    assert "ollama" in engines["available"]
    assert engines["degraded"] is False
    assert client.get("/ready").json()["checks"]["integrations"]["ollama"] == "configured"


def test_check_returns_status_without_provider_body(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.put(
        "/api/v1/settings/providers/groq",
        json={"fields": {"api_key": SAMPLE_GROQ}},
    ).status_code == 200

    def handler(request: httpx.Request) -> httpx.Response:
        assert SAMPLE_GROQ not in str(request.url)
        assert request.headers["authorization"] == f"Bearer {SAMPLE_GROQ}"
        return httpx.Response(401, json={"error": "bad key", "api_key": SAMPLE_GROQ, "raw": "provider-body"})

    def fake_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.settings.providers.http_client", fake_client)
    checked = client.post("/api/v1/settings/providers/groq/check")
    assert checked.status_code == 200
    assert checked.json() == {"ok": False, "status": "auth_failed"}
    assert SAMPLE_GROQ not in checked.text
    assert "provider-body" not in checked.text


def test_ollama_check_does_not_echo_the_tag_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "secret-model"}]})

    def fake_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.settings.providers.http_client", fake_client)
    checked = client.post("/api/v1/settings/providers/ollama/check")
    assert checked.status_code == 200
    body = checked.json()
    assert body["status"] == "reachable"
    assert body["model_present"] is True
    assert "secret-model" not in checked.text


def test_bad_ollama_address_is_rejected(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings/providers/ollama",
        json={"fields": {"base_url": "http://user:secret@127.0.0.1:11434"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "base_url must not include credentials."
    assert "user:secret" not in response.text


def test_missing_provider_is_404(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings/providers/openai",
        json={"fields": {"api_key": SAMPLE_GROQ}},
    )
    assert response.status_code == 404
    assert SAMPLE_GROQ not in response.text


def test_unauthenticated_requests_cannot_read_keys(client: TestClient) -> None:
    denied = client.get("/api/v1/settings/providers", headers={"X-Jarvis-Passcode": ""})
    assert denied.status_code == 401
    reveal = client.post(
        "/api/v1/settings/providers/groq/reveal",
        json={"field": "api_key"},
        headers={"X-Jarvis-Passcode": ""},
    )
    assert reveal.status_code == 401


def test_keys_page_does_not_touch_browser_storage() -> None:
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    start = script.index("// --- provider settings")
    end = script.index("// --- end provider settings ---")
    block = script[start:end]
    assert "localStorage" not in block
    assert "sessionStorage" not in block
    assert "document.cookie" not in block

    page = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'id="gateway-heading"' in page
    assert 'id="keys-heading"' in page
    assert "OpenAI" not in page
    assert "Anthropic" not in page
