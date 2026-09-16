"""API tests — full request path with no provider keys (echo engine).

Also guards the auth gate, since this service is deployed to a public URL.
"""

from __future__ import annotations

import json

from tests.conftest import TEST_DATABASE_URL, TEST_PASSCODE


# --- health ---------------------------------------------------------------


def test_health_is_open_and_reports_real_state(client) -> None:
    """/health is intentionally unauthenticated so uptime probes work."""
    response = client.get("/health", headers={"X-Jarvis-Passcode": ""})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"

    # Whichever backend the suite is running against, /health must report it
    # accurately -- the durability warning in the UI depends on this.
    expected_backend = "postgres" if TEST_DATABASE_URL else "sqlite"
    assert body["storage"] == expected_backend
    assert body["storage_durable"] is bool(TEST_DATABASE_URL)
    assert body["auth_enabled"] is True
    assert body["engines"]["degraded"] is True  # no keys in tests
    assert "echo" in body["engines"]["available"]
    assert body["telemetry"]["turns_logged"] == 0


# --- auth -----------------------------------------------------------------


def test_api_requires_passcode(client) -> None:
    assert client.post("/api/v1/session/start", json={}, headers={"X-Jarvis-Passcode": "wrong"}).status_code == 401
    assert client.get("/api/v1/greeting", headers={"X-Jarvis-Passcode": ""}).status_code == 401


def test_websocket_rejects_bad_passcode(client) -> None:
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/logs?passcode=wrong") as ws:
            ws.receive_text()


# --- session --------------------------------------------------------------


def test_session_lifecycle(client) -> None:
    created = client.post("/api/v1/session/start", json={"client_type": "IOS_PWA", "user_agent": "pytest"})
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    found = client.get(f"/api/v1/session/{session_id}")
    assert found.status_code == 200 and found.json()["exists"] is True

    missing = client.get("/api/v1/session/sess_nope")
    assert missing.status_code == 200 and missing.json()["exists"] is False


def test_command_rejects_unknown_session(client) -> None:
    response = client.post("/api/v1/command", json={"session_id": "sess_nope", "prompt": "hi"})
    assert response.status_code == 404


# --- greeting -------------------------------------------------------------


def test_greeting_exposes_mood_scale(client) -> None:
    body = client.get("/api/v1/greeting").json()
    assert body["greeting"] in {"Good morning", "Good afternoon", "Good evening"}
    assert [item["score"] for item in body["mood_scale"]] == [1, 2, 3, 4, 5]
    assert body["mood_scale"][0]["label"] == "rough"


# --- the core loop --------------------------------------------------------


def test_hello_turn_round_trips(client, session_id: str) -> None:
    response = client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hi"})
    assert response.status_code == 200

    body = response.json()
    assert body["intent"] == "PERSONAL_CHAT"
    assert body["provider"] == "ECHO"
    assert body["degraded"] is True
    assert body["trajectory_id"].startswith("traj_")
    assert len(body["response"]) > 0
    assert body["latency_ms"] >= 0


def test_unsupported_intent_still_answers_and_logs(client, session_id: str) -> None:
    """Phase 1 has no shell. The turn must be answered and logged, not 500."""
    response = client.post(
        "/api/v1/command", json={"session_id": session_id, "prompt": "run the command ls -la"}
    )
    assert response.status_code == 200
    assert response.json()["intent"] == "OS_AUTOMATION"

    assert client.get("/api/v1/telemetry/stats").json()["turns_logged"] == 1


def test_prompt_validation(client, session_id: str) -> None:
    assert client.post("/api/v1/command", json={"session_id": session_id, "prompt": ""}).status_code == 422


# --- mood -----------------------------------------------------------------


def test_mood_checkin_records_and_replies(client, session_id: str) -> None:
    response = client.post(
        "/api/v1/mood", json={"session_id": session_id, "score": 2, "note": "didn't sleep well"}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["score"] == 2
    assert body["label"] == "low"
    assert body["emoji"] == "🙁"
    assert body["checkin_id"].startswith("mood_")
    assert len(body["response"]) > 0


def test_mood_score_is_range_checked(client, session_id: str) -> None:
    for bad_score in (0, 6, -1):
        response = client.post("/api/v1/mood", json={"session_id": session_id, "score": bad_score})
        assert response.status_code == 422


def test_mood_history_returns_entries_newest_first(client, session_id: str) -> None:
    for score in (1, 3, 5):
        client.post("/api/v1/mood", json={"session_id": session_id, "score": score})

    body = client.get("/api/v1/mood/history?limit=10").json()
    assert body["count"] == 3
    assert body["average"] == 3.0
    assert body["entries"][0]["mood_score"] == 5    # newest first


def test_empty_mood_history_has_no_average(client) -> None:
    body = client.get("/api/v1/mood/history").json()
    assert body["count"] == 0
    assert body["average"] is None


# --- feedback & dataset ---------------------------------------------------


def test_feedback_then_dataset_export(client, session_id: str) -> None:
    turn = client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hello"}).json()

    rated = client.post("/api/v1/feedback", json={"trajectory_id": turn["trajectory_id"], "rating": 1})
    assert rated.status_code == 200 and rated.json()["rating"] == 1

    assert client.post(
        "/api/v1/feedback", json={"trajectory_id": "traj_nope", "rating": 1}
    ).status_code == 404

    export = client.get("/api/v1/dataset/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in export.text.strip().split("\n") if line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["metadata"]["user_reward"] == 1
    assert record["conversations"][0]["value"] == "hello"


def test_feedback_rating_is_range_checked(client, session_id: str) -> None:
    turn = client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hi"}).json()
    response = client.post("/api/v1/feedback", json={"trajectory_id": turn["trajectory_id"], "rating": 7})
    assert response.status_code == 422


# --- missions, agents, tools ---------------------------------------------


def test_agents_and_tools_are_listed(client) -> None:
    agents = client.get("/api/v1/agents").json()
    ids = {a["id"] for a in agents["agents"]}
    assert ids == {"assistant", "memory", "operator", "planner", "research"}
    assert agents["count"] == len(ids)

    tools = client.get("/api/v1/tools").json()
    by_name = {t["name"]: t for t in tools["tools"]}
    assert by_name["memory.forget"]["risk"] == "DANGEROUS"
    assert by_name["web.fetch"]["risk"] == "SAFE"
    assert by_name["memory.write"]["capability"] == "WRITE"

    # Node tools are declared centrally but execute on a machine in the fleet.
    assert by_name["shell.exec"]["requires_node"] is True
    assert by_name["shell.exec"]["risk"] == "DANGEROUS"
    assert by_name["memory.read"]["requires_node"] is False


def test_mission_runs_end_to_end_on_the_echo_engine(client) -> None:
    """With no API key the mission still plans, runs, and completes."""
    created = client.post(
        "/api/v1/missions", json={"goal": "say hello", "wait_seconds": 20}
    )
    assert created.status_code == 201

    body = created.json()
    mission_id = body["mission_id"]
    assert body["goal"] == "say hello"
    assert body["progress"]["total"] >= 1

    fetched = client.get(f"/api/v1/missions/{mission_id}").json()
    assert fetched["status"] in ("COMPLETED", "RUNNING", "FAILED")
    assert len(fetched["tasks"]) >= 1
    assert fetched["tasks"][0]["agent_id"] in {"assistant", "memory", "planner", "research"}


def test_mission_messages_are_auditable_over_the_api(client) -> None:
    mission_id = client.post(
        "/api/v1/missions", json={"goal": "audit me", "wait_seconds": 20}
    ).json()["mission_id"]

    messages = client.get(f"/api/v1/missions/{mission_id}/messages").json()
    assert messages["count"] >= 2
    assert {"mission.create", "task.dispatch"} <= {m["action"] for m in messages["messages"]}


def test_mission_list_and_unknown_id(client) -> None:
    client.post("/api/v1/missions", json={"goal": "one", "wait_seconds": 10})
    listing = client.get("/api/v1/missions?limit=5").json()
    assert listing["count"] >= 1

    assert client.get("/api/v1/missions/msn_nope").status_code == 404
    assert client.get("/api/v1/missions/msn_nope/messages").status_code == 404


def test_missions_require_auth(client) -> None:
    for path in ("/api/v1/missions", "/api/v1/agents", "/api/v1/tools", "/api/v1/approvals"):
        assert client.get(path, headers={"X-Jarvis-Passcode": "wrong"}).status_code == 401


def test_approval_endpoints(client) -> None:
    from app.missions import store
    from app.missions.models import Mission, new_mission_id

    mission_id = new_mission_id()
    store.create_mission(Mission(mission_id=mission_id, goal="approve me"))
    approval_id = store.create_approval(
        mission_id=mission_id, task_id="task_1", agent_id="memory",
        tool_name="memory.forget", tool_arguments={"scope": "user", "key": "city"},
        risk="DANGEROUS",
    )

    pending = client.get("/api/v1/approvals").json()
    assert pending["count"] == 1
    assert pending["approvals"][0]["tool_name"] == "memory.forget"

    decided = client.post(f"/api/v1/approvals/{approval_id}", json={"approve": True})
    assert decided.status_code == 200 and decided.json()["decision"] == "APPROVED"

    # Deciding twice is refused rather than silently overwriting.
    assert client.post(f"/api/v1/approvals/{approval_id}", json={"approve": False}).status_code == 404
    assert client.get("/api/v1/approvals").json()["count"] == 0


def test_mission_goal_is_validated(client) -> None:
    assert client.post("/api/v1/missions", json={"goal": ""}).status_code == 422


# --- websocket & PWA ------------------------------------------------------


def test_websocket_streams_turn_telemetry(client, session_id: str) -> None:
    with client.websocket_connect(f"/ws/logs?passcode={TEST_PASSCODE}") as ws:
        assert json.loads(ws.receive_text())["event"] == "system"

        client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hi"})

        events = [json.loads(ws.receive_text()) for _ in range(2)]
        kinds = [event["event"] for event in events]
        assert "orchestrator" in kinds
        assert "telemetry" in kinds

        telemetry_event = next(e for e in events if e["event"] == "telemetry")
        assert telemetry_event["provider"] == "ECHO"
        assert telemetry_event["trajectory_id"].startswith("traj_")


def test_pwa_shell_is_served(client) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "Jarvis" in index.text

    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    assert manifest.json()["short_name"] == "Jarvis"
    assert manifest.json()["display"] == "standalone"

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "application/javascript" in worker.headers["content-type"]

    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/icons/icon-192.png").status_code == 200
