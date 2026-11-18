"""The API contract the iOS app (ios/Jarvis) depends on.

Every request body here is byte-for-byte what the Swift client encodes
(ios/Jarvis/Core/Models.swift), and every response field read here is one the
Swift models decode. The app is built on a Mac and can't run in this suite, so
this is the half of the contract that can be enforced on every commit: change
a field name the app relies on, and this fails here instead of on the phone.
"""

from __future__ import annotations


def _session(client) -> str:  # noqa: ANN001
    response = client.post("/api/v1/session/start", json={"client_type": "IOS_APP"})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_todo_round_trip_matches_the_swift_models(client):
    client.post("/api/v1/todos/tracks/jarvis", json={"is_active": True})

    created = client.post(
        "/api/v1/todos", json={"title": "from the phone", "track": "jarvis", "status": "inbox"}
    )
    assert created.status_code == 201
    todo = created.json()["todo"]
    for field in ("todo_id", "title", "track", "status", "priority", "notes", "due_at"):
        assert field in todo

    done = client.patch(f"/api/v1/todos/{todo['todo_id']}", json={"status": "done"})
    assert done.json()["todo"]["status"] == "done"

    listed = client.get("/api/v1/todos").json()
    assert isinstance(listed["todos"], list)


def test_next_and_summary_fields(client):
    next_body = client.get("/api/v1/todos/next").json()
    assert {"todo", "active_tracks", "reason"} <= next_body.keys()

    summary = client.get("/api/v1/todos/summary").json()
    assert {"active_tracks", "verdict", "open_total"} <= summary.keys()


def test_chat_and_mood_with_a_session(client):
    session_id = _session(client)

    reply = client.post("/api/v1/command", json={"session_id": session_id, "prompt": "hi"}).json()
    assert {"response", "provider", "degraded"} <= reply.keys()

    mood = client.post(
        "/api/v1/mood", json={"session_id": session_id, "score": 4, "note": ""}
    ).json()
    assert {"score", "label", "response"} <= mood.keys()


def test_a_stale_session_is_a_404_so_the_app_can_start_a_new_one(client):
    # JarvisClient.withSession retries exactly once on 404 — that only works if
    # an unknown session is a 404, not a 400 or a 500.
    response = client.post("/api/v1/command", json={"session_id": "sess_gone", "prompt": "hi"})
    assert response.status_code == 404


def test_wrong_passcode_is_401_with_a_detail(client):
    response = client.get("/api/v1/todos/summary", headers={"X-Jarvis-Passcode": "wrong"})
    assert response.status_code == 401
    assert "detail" in response.json()


def test_approval_fields_and_decision(client):
    from app.missions import store as mission_store

    approval_id = mission_store.create_approval(
        mission_id="mis_1", task_id="task_1", agent_id="operator", tool_name="fs.delete",
        tool_arguments={"path": "~/ws/old.log", "max_bytes": 10}, risk="DANGEROUS",
    )

    approvals = client.get("/api/v1/approvals").json()["approvals"]
    assert {
        "approval_id", "mission_id", "agent_id", "tool_name", "tool_arguments", "risk", "created_at"
    } <= approvals[0].keys()
    # Argument names arrive exactly as the agent sent them.
    assert "max_bytes" in approvals[0]["tool_arguments"]

    decided = client.post(f"/api/v1/approvals/{approval_id}", json={"approve": False})
    assert decided.status_code == 200


def test_device_report_as_the_app_sends_it(client):
    response = client.post(
        "/api/v1/devices/checkin",
        json={
            "device_id": "ios-2f1c",
            "name": "iPhone",
            "platform": "ios",
            "model": "iPhone17,3",
            "os_version": "26.6.1",
            "app_version": "0.1",
            "via": "shortcut:arrived_office",
            "metrics": {
                "battery_level": 0.82,
                "battery_state": "unplugged",
                "low_power_mode": False,
                "thermal_state": "nominal",
                "uptime_hours": 51.3,
                "timezone": "Asia/Kolkata",
                "disk_free_gb": 41.7,
                "network": "wifi",
                "network_expensive": False,
                "network_constrained": False,
            },
        },
    )
    assert response.status_code == 201
