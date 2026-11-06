"""SEC-001: the process must not serve an open API by accident.

Proves startup refusal, a 401 on every protected route, login lockout, and
rejection of expired or revoked session tokens.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import StartupRefused
from tests.conftest import TEST_PASSCODE

NO_AUTH = {"X-Jarvis-Passcode": ""}

# The lock screen has to load, and uptime probes have to run. Nothing else.
_PUBLIC_EXACT = {"/health", "/", "/favicon.ico", "/manifest.json", "/sw.js"}


def _clear_settings() -> None:
    from app.core.config import get_settings
    from app.core.security import reset_auth_state

    get_settings.cache_clear()
    reset_auth_state()


def _prepare_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str | None) -> None:
    monkeypatch.delenv("JARVIS_PASSCODE", raising=False)
    monkeypatch.delenv("JARVIS_DEV_INSECURE", raising=False)
    monkeypatch.delenv("JARVIS_ALLOW_PUBLIC_BIND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_HOST", "127.0.0.1")
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "boot.db"))
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    _clear_settings()


def _raise_cause(exc: BaseException) -> StartupRefused | None:
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, StartupRefused):
            return current
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
        if hasattr(current, "exceptions"):
            stack.extend(current.exceptions)
    return None


def _expect_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, match: str, **env: str | None) -> None:
    import app.main as main_module

    _prepare_boot(monkeypatch, tmp_path, **env)
    with pytest.raises(BaseException) as caught:
        with TestClient(main_module.app):
            pass
    refused = _raise_cause(caught.value)
    assert refused is not None, f"startup did not refuse, got {caught.value!r}"
    assert match in str(refused)


# --- boot policy ----------------------------------------------------------


def test_default_bind_and_session_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_HOST", raising=False)
    monkeypatch.delenv("JARVIS_SESSION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_ALLOW_PUBLIC_BIND", raising=False)
    _clear_settings()
    from app.core.config import get_settings
    from app.core.security import is_loopback

    settings = get_settings()
    assert settings.host == "127.0.0.1"
    assert is_loopback(settings.host)
    assert settings.allow_public_bind is False
    assert settings.session_ttl_seconds == 12 * 60 * 60
    _clear_settings()


def test_refuses_to_start_without_a_passcode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _expect_refused(tmp_path, monkeypatch, "JARVIS_PASSCODE")


def test_dev_insecure_on_a_public_bind_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dev exception is loopback-only, even if public bind is also opted into."""
    _expect_refused(
        tmp_path,
        monkeypatch,
        "JARVIS_PASSCODE",
        JARVIS_DEV_INSECURE="1",
        JARVIS_ALLOW_PUBLIC_BIND="1",
        JARVIS_HOST="0.0.0.0",
    )


def test_public_bind_without_the_flag_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _expect_refused(
        tmp_path,
        monkeypatch,
        "JARVIS_ALLOW_PUBLIC_BIND",
        JARVIS_PASSCODE="secret",
        JARVIS_HOST="0.0.0.0",
    )


def test_dev_insecure_loopback_starts_and_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import app.main as main_module

    _prepare_boot(monkeypatch, tmp_path, JARVIS_DEV_INSECURE="1", JARVIS_HOST="127.0.0.1")
    with caplog.at_level(logging.WARNING):
        with TestClient(main_module.app) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["auth_enabled"] is False
            assert client.get("/api/v1/greeting").status_code == 200
    assert "authentication is DISABLED" in caplog.text
    assert "JARVIS_DEV_INSECURE=1" in caplog.text


def test_public_bind_with_flag_and_passcode_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import app.main as main_module

    _prepare_boot(
        monkeypatch,
        tmp_path,
        JARVIS_PASSCODE="secret",
        JARVIS_ALLOW_PUBLIC_BIND="1",
        JARVIS_HOST="0.0.0.0",
    )
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/greeting").status_code == 401
        authed = client.get("/api/v1/greeting", headers={"X-Jarvis-Passcode": "secret"})
        assert authed.status_code == 200


def test_uvicorn_host_flag_is_the_bind_that_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uvicorn --host 0.0.0.0` must not slip past a loopback JARVIS_HOST."""
    from app.core.config import get_settings
    from app.core.security import StartupRefused, enforce_startup_policy, host_from_argv

    monkeypatch.setenv("JARVIS_PASSCODE", "secret")
    monkeypatch.setenv("JARVIS_HOST", "127.0.0.1")
    monkeypatch.delenv("JARVIS_ALLOW_PUBLIC_BIND", raising=False)
    _clear_settings()

    argv = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    assert host_from_argv(argv) == "0.0.0.0"
    with pytest.raises(StartupRefused, match="JARVIS_ALLOW_PUBLIC_BIND"):
        enforce_startup_policy(get_settings(), bind_host=host_from_argv(argv))

    dashed = ["python", "-m", "uvicorn", "app.main:app", "--host=127.0.0.1"]
    assert host_from_argv(dashed) == "127.0.0.1"
    # pytest's argv is not a bind address, even if it contains a --host lookalike.
    assert host_from_argv(["pytest", "tests/test_auth.py", "--host", "0.0.0.0"]) is None
    _clear_settings()


def test_passcode_mismatch_is_rejected_for_prefixes(client) -> None:
    """A prefix or a longer string must not compare equal. Length is hashed away."""
    from app.core.security import passcode_matches

    assert passcode_matches(TEST_PASSCODE) is True
    assert passcode_matches(TEST_PASSCODE[:-1]) is False
    assert passcode_matches(TEST_PASSCODE + "x") is False
    assert passcode_matches("") is False
    assert passcode_matches(None) is False


# --- route gate -----------------------------------------------------------


def _filled(path: str) -> str:
    replacements = {
        "session_id": "sess_missing",
        "mission_id": "msn_missing",
        "approval_id": "apr_missing",
        "entry_id": "mem_missing",
        "todo_id": "todo_missing",
        "track": "build",
        "node_id": "node_missing",
    }
    filled = path
    for name, value in replacements.items():
        filled = filled.replace("{" + name + "}", value)
    assert "{" not in filled, filled
    return filled


def test_every_protected_route_is_401_without_auth(client) -> None:
    import app.main as main_module

    seen: list[str] = []
    for route in main_module.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = _filled(route.path)
        if route.path in _PUBLIC_EXACT or route.path.startswith("/static/"):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            kwargs: dict = {"headers": NO_AUTH}
            if method in {"POST", "PUT", "PATCH"}:
                kwargs["json"] = {}
            response = client.request(method, path, **kwargs)
            assert response.status_code == 401, f"{method} {path} -> {response.status_code} {response.text}"
            assert response.json()["detail"]
            seen.append(f"{method} {route.path}")

    # The sweep has to actually cover the surface, not an empty router.
    assert "GET /api/v1/greeting" in seen
    assert "POST /api/v1/auth/login" in seen
    assert "POST /api/v1/auth/logout" in seen
    assert "GET /api/v1/nodes" in seen
    assert "POST /api/v1/nodes/dispatch" in seen
    assert "GET /api/v1/dataset/export" in seen

    for path in ("/docs", "/redoc", "/openapi.json", "/api/v1/not-a-route"):
        response = client.get(path, headers=NO_AUTH)
        assert response.status_code == 401, path

    for path in ("/health", "/", "/favicon.ico", "/manifest.json", "/sw.js", "/static/app.js"):
        response = client.get(path, headers=NO_AUTH)
        assert response.status_code == 200, path


def test_log_socket_and_node_socket_require_auth(client) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/logs") as ws:
            ws.receive_text()

    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "name": "impostor"}))
        assert json.loads(ws.receive_text())["type"] == "error"


# --- sessions and lockout -------------------------------------------------


def test_login_issues_a_bearer_token_that_authorizes(client) -> None:
    body = client.post("/api/v1/auth/login", json={"passcode": TEST_PASSCODE}).json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 12 * 60 * 60
    token = body["token"]

    bearer = {**NO_AUTH, "Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/greeting", headers=bearer).status_code == 200

    header = {**NO_AUTH, "X-Jarvis-Token": token}
    assert client.get("/api/v1/agents", headers=header).status_code == 200

    with client.websocket_connect(f"/ws/logs?token={token}") as ws:
        assert json.loads(ws.receive_text())["event"] == "system"


def test_logout_revokes_the_token_only(client) -> None:
    token = client.post("/api/v1/auth/login", json={"passcode": TEST_PASSCODE}).json()["token"]
    bearer = {**NO_AUTH, "Authorization": f"Bearer {token}"}
    assert client.post("/api/v1/auth/logout", headers=bearer).status_code == 200
    assert client.get("/api/v1/greeting", headers=bearer).status_code == 401
    # The passcode is the credential. Logout ends the session, not the secret.
    assert client.get("/api/v1/greeting", headers={"X-Jarvis-Passcode": TEST_PASSCODE}).status_code == 200


def test_expired_token_is_rejected(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.security as security
    from app.core.config import get_settings

    monkeypatch.setenv("JARVIS_SESSION_TTL_SECONDS", "60")
    get_settings.cache_clear()
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(security, "_now", lambda: clock["t"])

    body = client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    ).json()
    assert body["expires_in"] == 60
    token = body["token"]
    bearer = {**NO_AUTH, "Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/greeting", headers=bearer).status_code == 200

    clock["t"] += 60
    assert client.get("/api/v1/greeting", headers=bearer).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/logs?token={token}") as ws:
            ws.receive_text()

    get_settings.cache_clear()


def test_login_locks_out_after_n_failures(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.security as security
    from app.core.config import get_settings

    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(security, "_now", lambda: clock["t"])
    settings = get_settings()
    limit = settings.login_max_failures

    # A session opened before the attack stays usable through the lockout.
    token = client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    ).json()["token"]
    bearer = {**NO_AUTH, "Authorization": f"Bearer {token}"}

    for attempt in range(limit - 1):
        failed = client.post("/api/v1/auth/login", json={"passcode": "wrong"}, headers=NO_AUTH)
        assert failed.status_code == 401, attempt

    tripped = client.post("/api/v1/auth/login", json={"passcode": "wrong"}, headers=NO_AUTH)
    assert tripped.status_code == 429
    assert int(tripped.headers["retry-after"]) >= 1

    # The correct passcode does not bypass the window, and neither does the header.
    assert client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    ).status_code == 429
    assert client.get("/api/v1/greeting", headers={"X-Jarvis-Passcode": TEST_PASSCODE}).status_code == 429
    assert client.get("/api/v1/greeting", headers=bearer).status_code == 200

    clock["t"] += settings.login_backoff_seconds - 1
    assert client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    ).status_code == 429

    clock["t"] += 2
    released = client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    )
    assert released.status_code == 200
    assert released.json()["token"]


def test_pwa_exchanges_the_passcode_for_a_session_token() -> None:
    """The installed client must not keep sending the passcode on every call."""
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text()
    assert "/api/v1/auth/login" in source
    assert "/api/v1/auth/logout" in source
    assert "Authorization" in source
    assert "Bearer" in source
    assert "X-Jarvis-Passcode" not in source


def test_header_guesses_share_the_login_lockout(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.security as security
    from app.core.config import get_settings

    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(security, "_now", lambda: clock["t"])
    limit = get_settings().login_max_failures

    for _ in range(limit):
        client.get("/api/v1/greeting", headers={"X-Jarvis-Passcode": "nope"})

    locked = client.post(
        "/api/v1/auth/login", json={"passcode": TEST_PASSCODE}, headers=NO_AUTH
    )
    assert locked.status_code == 429
