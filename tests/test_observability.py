"""Correlation ids, structured logs, redaction, and readiness.

Secrets in this file are fake. They exist so the redaction filter can be
shown to remove them.
"""

from __future__ import annotations

import json
import logging

from app.core.redact import REDACTED, redact


def test_redact_strips_labeled_secrets_and_leaves_the_rest() -> None:
    text = (
        "login password=hunter2-not-a-real-password for the user "
        "api_key=sk-test-fake-token-0000 "
        'Authorization: Bearer fake-auth-token-xyz '
        "totp=654321 "
        '{"password": "json-fake-secret-999"} '
        "planned tasks: 3"
    )
    cleaned = redact(text)

    for secret in (
        "hunter2-not-a-real-password",
        "sk-test-fake-token-0000",
        "fake-auth-token-xyz",
        "654321",
        "json-fake-secret-999",
    ):
        assert secret not in cleaned
    assert cleaned.count(REDACTED) >= 5
    assert "login" in cleaned
    assert "planned tasks: 3" in cleaned
    assert redact(cleaned) == cleaned


def test_log_record_does_not_keep_a_fake_secret() -> None:
    from app.core.structured_log import JsonFormatter, RedactionFilter

    record = logging.LogRecord(
        name="jarvis.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="using password=%s",
        args=("hunter2-not-a-real-password",),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    line = JsonFormatter().format(record)
    body = json.loads(line)

    assert "hunter2-not-a-real-password" not in line
    assert body["level"] == "INFO"
    assert body["component"] == "jarvis.test"
    assert "timestamp" in body
    assert "correlation_id" in body
    assert "mission_id" in body
    assert REDACTED in body["message"]


def test_health_stays_a_liveness_probe(client) -> None:
    response = client.get("/health", headers={"X-Jarvis-Passcode": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "storage" in body
    assert "engines" in body
    assert "telemetry" in body
    assert response.headers["x-correlation-id"].startswith("cid_")


def test_ready_reports_database_scheduler_and_integrations(client) -> None:
    response = client.get("/ready", headers={"X-Jarvis-Passcode": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["scheduler"] == "ok"
    integrations = body["checks"]["integrations"]
    assert integrations["groq"] == "unconfigured"
    assert integrations["gemini"] == "unconfigured"
    assert integrations["search"] == "unconfigured"
    assert integrations["telegram"] == "unconfigured"
    assert integrations["echo"] == "configured"
    assert "password" not in response.text
    assert "://" not in response.text


def test_ready_is_not_ready_when_the_database_ping_fails(client, monkeypatch) -> None:
    import app.main as main_module

    monkeypatch.setattr(main_module, "ping_database", lambda: False)
    response = client.get("/ready", headers={"X-Jarvis-Passcode": ""})
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error"
    assert body["checks"]["scheduler"] == "ok"


def test_missing_correlation_header_is_assigned(client) -> None:
    response = client.get("/health")
    correlation_id = response.headers["x-correlation-id"]
    assert correlation_id.startswith("cid_")
    assert " " not in correlation_id


def test_invalid_correlation_header_is_replaced(client) -> None:
    response = client.get("/health", headers={"X-Correlation-ID": "bad id"})
    correlation_id = response.headers["x-correlation-id"]
    assert correlation_id != "bad id"
    assert " " not in correlation_id
    assert correlation_id.startswith("cid_")


def test_init_db_is_safe_to_repeat_and_keeps_the_id(env) -> None:
    from app.missions import store
    from app.missions.models import Mission, new_mission_id

    store.init_db()
    store.init_db()
    mission = Mission(
        mission_id=new_mission_id(),
        goal="reinit",
        correlation_id="cid_reinit",
    )
    store.create_mission(mission)
    loaded = store.get_mission(mission.mission_id)
    assert loaded is not None
    assert loaded.correlation_id == "cid_reinit"


def test_existing_tables_gain_the_correlation_column(env) -> None:
    """A database created before the column existed must still boot."""
    from app.core.db import execute, fetch_one, get_conn
    from app.missions import store

    with get_conn() as conn:
        execute(
            conn,
            """
            CREATE TABLE missions (
                mission_id TEXT PRIMARY KEY, session_id TEXT, created_at TEXT,
                updated_at TEXT, goal TEXT, status TEXT, budget TEXT,
                summary TEXT, error TEXT
            )
            """,
        )
        execute(
            conn,
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, mission_id TEXT, created_at TEXT,
                updated_at TEXT, agent_id TEXT, objective TEXT, status TEXT,
                depth INTEGER, depends_on TEXT, result TEXT, error TEXT,
                attempts INTEGER, idempotency_key TEXT, lease_expires_at TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER, tool_calls INTEGER
            )
            """,
        )
        execute(
            conn,
            """
            CREATE TABLE agent_messages (
                message_id TEXT PRIMARY KEY, mission_id TEXT, task_id TEXT,
                created_at TEXT, sender TEXT, receiver TEXT, action TEXT,
                payload TEXT, status TEXT
            )
            """,
        )

    store.init_db()

    with get_conn() as conn:
        for table in ("missions", "tasks", "agent_messages"):
            row = fetch_one(conn, f"SELECT correlation_id FROM {table} LIMIT 1")
            assert row is None or "correlation_id" in row


def test_one_request_id_traces_a_full_mission(client) -> None:
    """The id on the request is the id on the mission, its tasks, its audit
    rows, and the scheduler, runner, and engine log lines for that run."""
    from app.core.structured_log import ContextFilter, JsonFormatter, RedactionFilter

    correlation_id = "cid-trace-test-001"
    captured = _CapturingHandler()
    captured.addFilter(ContextFilter())
    captured.addFilter(RedactionFilter())
    captured.setFormatter(JsonFormatter())
    logger = logging.getLogger("jarvis")
    logger.addHandler(captured)
    try:
        created = client.post(
            "/api/v1/missions",
            json={"goal": "say hello", "wait_seconds": 20},
            headers={"X-Correlation-ID": correlation_id},
        )
    finally:
        logger.removeHandler(captured)

    assert created.status_code == 201
    assert created.headers["x-correlation-id"] == correlation_id
    body = created.json()
    mission_id = body["mission_id"]
    assert body["correlation_id"] == correlation_id
    assert body["status"] == "COMPLETED"
    assert body["tasks"]
    assert all(task["correlation_id"] == correlation_id for task in body["tasks"])

    messages = client.get(f"/api/v1/missions/{mission_id}/messages").json()
    assert messages["count"] >= 1
    assert all(row["correlation_id"] == correlation_id for row in messages["messages"])

    records = [json.loads(line) for line in captured.lines]
    traced = [
        record for record in records
        if record["correlation_id"] == correlation_id and record["mission_id"] == mission_id
    ]
    components = {record["component"] for record in traced}
    assert "jarvis.scheduler" in components
    assert "jarvis.runner" in components
    assert "jarvis.engines" in components
    assert any(record["message"] == "mission started" for record in traced)
    assert any(record["message"].startswith("agent step") for record in traced)
    assert any(record["message"].startswith("adapter call") for record in traced)


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
