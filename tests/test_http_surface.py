"""SEC-002: the HTTP surface is an allowlist, not an open socket.

CORS grants, browser headers, body size, upload types, rate limits, and
what an error is allowed to say.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.http_guard import CONTENT_SECURITY_POLICY
from tests.conftest import TEST_PASSCODE

EVIL = "https://evil.example"
IONIC = "http://localhost:8100"


def _boot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    raise_server_exceptions: bool = True,
    **env: str | None,
) -> TestClient:
    """A client whose middleware saw this env. CORS is fixed at import."""
    monkeypatch.setenv("JARVIS_PASSCODE", TEST_PASSCODE)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "http.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JARVIS_DEBUG", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CORS_ORIGINS", raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.agent.orchestrator as orchestrator_module
    import app.main as main_module

    importlib.reload(orchestrator_module)
    importlib.reload(main_module)
    client = TestClient(main_module.app, raise_server_exceptions=raise_server_exceptions)
    client.__enter__()
    client.headers.update({"X-Jarvis-Passcode": TEST_PASSCODE})
    return client


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _cors_grant(response) -> tuple[str | None, str | None]:
    return (
        response.headers.get("access-control-allow-origin"),
        response.headers.get("access-control-allow-credentials"),
    )


# --- CORS -----------------------------------------------------------------


def test_unlisted_origin_is_not_granted(client) -> None:
    response = client.get("/health", headers={"Origin": EVIL})
    assert response.status_code == 200
    origin, credentials = _cors_grant(response)
    assert origin is None
    assert credentials is None

    preflight = client.options(
        "/api/v1/greeting",
        headers={
            "Origin": EVIL,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Jarvis-Passcode",
        },
    )
    origin, credentials = _cors_grant(preflight)
    assert origin != EVIL
    assert not (origin == "*" and credentials == "true")


def test_allowlisted_origin_is_granted_with_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _boot(monkeypatch, tmp_path, JARVIS_CORS_ORIGINS=IONIC)
    try:
        response = client.get("/health", headers={"Origin": IONIC})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == IONIC
        assert response.headers["access-control-allow-credentials"] == "true"

        preflight = client.options(
            "/api/v1/greeting",
            headers={
                "Origin": IONIC,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Jarvis-Passcode, Content-Type",
            },
        )
        assert preflight.headers["access-control-allow-origin"] == IONIC
        assert preflight.headers["access-control-allow-credentials"] == "true"
        assert "x-jarvis-passcode" in preflight.headers["access-control-allow-headers"].lower()

        rejected = client.get("/health", headers={"Origin": EVIL})
        origin, credentials = _cors_grant(rejected)
        assert origin != EVIL
        assert not (origin == "*" and credentials == "true")
    finally:
        _close(client)


def test_wildcard_is_never_paired_with_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`*` used to be the default, with credentials on. That combination is refused."""
    client = _boot(monkeypatch, tmp_path, JARVIS_CORS_ORIGINS="*")
    try:
        response = client.get("/health", headers={"Origin": EVIL})
        origin, credentials = _cors_grant(response)
        assert origin != "*"
        assert origin != EVIL
        assert not (origin == "*" and credentials == "true")
        assert credentials != "true"
    finally:
        _close(client)


def test_mixed_wildcard_keeps_only_explicit_origins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _boot(monkeypatch, tmp_path, JARVIS_CORS_ORIGINS=f"*, {IONIC}")
    try:
        granted = client.get("/health", headers={"Origin": IONIC})
        assert granted.headers["access-control-allow-origin"] == IONIC
        assert granted.headers["access-control-allow-credentials"] == "true"
        refused = client.get("/health", headers={"Origin": EVIL})
        assert _cors_grant(refused)[0] != EVIL
    finally:
        _close(client)


# --- headers --------------------------------------------------------------


def _assert_browser_policy(response) -> None:
    policy = response.headers["content-security-policy"]
    assert policy == CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in policy
    assert "default-src 'self'" in policy
    assert "*" not in policy
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_security_headers_on_html_and_api(client) -> None:
    _assert_browser_policy(client.get("/health"))
    page = client.get("/")
    assert page.status_code == 200
    _assert_browser_policy(page)
    _assert_browser_policy(client.get("/api/v1/greeting"))


def test_pwa_does_not_need_inline_style_or_script() -> None:
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    assert "style=" not in (root / "index.html").read_text()
    assert "style=" not in (root / "app.js").read_text()
    html = (root / "index.html").read_text()
    assert "<script>" not in html
    assert html.count("<script ") == 1


# --- body size and uploads ------------------------------------------------


def test_oversize_body_is_rejected(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_MAX_BODY_BYTES", "64")
    from app.core.config import get_settings

    get_settings.cache_clear()
    response = client.post(
        "/api/v1/session/start",
        json={"user_agent": "x" * 200},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large."
    _assert_browser_policy(response)
    get_settings.cache_clear()


def test_upload_content_types_are_rejected(client) -> None:
    """No route accepts a file. A file-shaped body is a type error, not a parse."""
    png = client.post(
        "/api/v1/session/start",
        content=b"\x89PNG\r\n",
        headers={"Content-Type": "image/png"},
    )
    assert png.status_code == 415
    assert png.json()["detail"] == "This server does not accept file uploads."

    form = client.post(
        "/api/v1/session/start",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert form.status_code == 415


def test_json_under_the_cap_still_works(client) -> None:
    response = client.post("/api/v1/session/start", json={"client_type": "WEB_PWA"})
    assert response.status_code == 201


# --- rate limits ----------------------------------------------------------


def test_mission_start_rate_limit(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.http_guard as guard
    from app.core.config import get_settings

    monkeypatch.setenv("JARVIS_MISSION_RATE_LIMIT", "2")
    monkeypatch.setenv("JARVIS_RATE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    guard.reset_rate_limits()
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(guard, "_now", lambda: clock["t"])

    for _ in range(2):
        created = client.post("/api/v1/missions", json={"goal": "say hello"})
        assert created.status_code == 201, created.text

    blocked = client.post("/api/v1/missions", json={"goal": "say hello again"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.json()["detail"] == "Too many requests. Try again later."
    assert client.get("/api/v1/missions").json()["count"] == 2

    clock["t"] += 61
    released = client.post("/api/v1/missions", json={"goal": "after the window"})
    assert released.status_code == 201
    get_settings.cache_clear()


def test_llm_endpoint_rate_limit(client, session_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.http_guard as guard
    from app.core.config import get_settings

    monkeypatch.setenv("JARVIS_LLM_RATE_LIMIT", "2")
    get_settings.cache_clear()
    guard.reset_rate_limits()

    for _ in range(2):
        ok = client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hi"})
        assert ok.status_code == 200, ok.text

    blocked = client.post("/api/v1/mood", json={"session_id": session_id, "score": 3})
    assert blocked.status_code == 429
    # A missing session is rejected before the limiter, so it must not be a 429.
    missing = client.post(
        "/api/v1/command", json={"session_id": "sess_nope", "prompt": "hi"}
    )
    assert missing.status_code == 404
    get_settings.cache_clear()


# --- errors ---------------------------------------------------------------


def test_engine_failure_hides_the_exception(client, session_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    secret = "postgresql://jarvis:secret-token@localhost/jarvis"

    async def explode(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(main_module.orchestrator, "run_turn", explode)
    response = client.post(
        "/api/v1/command", json={"session_id": session_id, "prompt": "hi"}
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Engine failure."
    assert "secret-token" not in response.text
    assert "postgresql" not in response.text
    assert "Traceback" not in response.text


def test_engine_failure_in_debug_includes_the_message(
    client, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module
    from app.core.config import get_settings

    monkeypatch.setenv("JARVIS_DEBUG", "1")
    get_settings.cache_clear()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("provider said no")

    monkeypatch.setattr(main_module.orchestrator, "run_turn", explode)
    response = client.post(
        "/api/v1/command", json={"session_id": session_id, "prompt": "hi"}
    )
    assert response.status_code == 502
    assert "provider said no" in response.json()["detail"]
    get_settings.cache_clear()


def test_unhandled_error_response_is_generic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "sk-live-secret DATABASE_URL=postgresql://jarvis:secret@localhost/jarvis"

    def boom() -> str:
        raise RuntimeError(secret + "\nTraceback (most recent call last):\n  File fake")

    from app.agent import prompts

    monkeypatch.setattr(prompts, "greeting_for", boom)
    client = _boot(monkeypatch, tmp_path, raise_server_exceptions=False)
    try:
        response = client.get("/api/v1/greeting")
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error."}
        assert "sk-live" not in response.text
        assert "postgresql" not in response.text
        assert "Traceback" not in response.text
        _assert_browser_policy(response)
    finally:
        _close(client)


def test_unhandled_error_in_debug_includes_the_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom() -> str:
        raise RuntimeError("boom-marker")

    from app.agent import prompts

    monkeypatch.setattr(prompts, "greeting_for", boom)
    client = _boot(monkeypatch, tmp_path, raise_server_exceptions=False, JARVIS_DEBUG="1")
    try:
        response = client.get("/api/v1/greeting")
        assert response.status_code == 500
        body = response.json()
        assert "boom-marker" in body["error"]
        assert "Traceback" in body["trace"]
    finally:
        _close(client)


def test_health_telemetry_failure_does_not_echo_the_exception(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    def explode() -> dict:
        raise RuntimeError("password=supersecret host=db.internal")

    monkeypatch.setattr(main_module.telemetry, "stats", explode)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["telemetry_error"] == "unavailable"
    assert "supersecret" not in response.text
    assert "db.internal" not in response.text
