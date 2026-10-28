"""Device check-ins: the iPhone reporting on itself (D-013)."""

from __future__ import annotations

from tests.conftest import TEST_PASSCODE


def _checkin(client, device_id="iphone-1", **extra):  # noqa: ANN001
    body = {
        "device_id": device_id,
        "name": "Dhruv's iPhone",
        "platform": "ios",
        "model": "iPhone17,3",
        "os_version": "26.6.1",
        "app_version": "0.1",
        "via": "app_open",
        "metrics": {"battery_level": 0.82, "charging": False, "network": "wifi"},
    }
    body.update(extra)
    return client.post("/api/v1/devices/checkin", json=body)


def test_checkin_is_stored_with_its_metrics(client):
    response = _checkin(client)

    assert response.status_code == 201
    report = response.json()["report"]
    assert report["device_id"] == "iphone-1"
    assert report["via"] == "app_open"
    assert report["metrics"]["battery_level"] == 0.82


def test_checkin_requires_the_passcode(client):
    client.headers.pop("X-Jarvis-Passcode")
    try:
        assert _checkin(client).status_code == 401
    finally:
        client.headers["X-Jarvis-Passcode"] = TEST_PASSCODE


def test_nested_metrics_are_refused(client):
    response = _checkin(client, metrics={"health": {"steps": 9000}})
    assert response.status_code == 400
    assert "health" in response.json()["detail"]


def test_too_many_metrics_are_refused(client):
    response = _checkin(client, metrics={f"m{i}": i for i in range(65)})
    assert response.status_code == 400


def test_every_checkin_is_kept_and_read_newest_first(client):
    _checkin(client, metrics={"battery_level": 0.9})
    _checkin(client, metrics={"battery_level": 0.6})
    _checkin(client, metrics={"battery_level": 0.3})

    reports = client.get("/api/v1/devices/iphone-1/reports").json()["reports"]

    assert [r["metrics"]["battery_level"] for r in reports] == [0.3, 0.6, 0.9]


def test_device_list_shows_latest_report_per_device(client):
    _checkin(client, device_id="iphone-1", metrics={"battery_level": 0.9})
    _checkin(client, device_id="iphone-1", metrics={"battery_level": 0.4})
    _checkin(client, device_id="ipad-1", metrics={"battery_level": 1.0})

    devices = {d["device_id"]: d for d in client.get("/api/v1/devices").json()["devices"]}

    assert devices["iphone-1"]["metrics"]["battery_level"] == 0.4
    assert devices["iphone-1"]["reports"] == 2
    assert devices["ipad-1"]["reports"] == 1


def test_forgetting_a_device_wipes_its_history(client):
    _checkin(client)
    _checkin(client)

    response = client.delete("/api/v1/devices/iphone-1")

    assert response.json()["removed"] == 2
    assert client.get("/api/v1/devices").json()["count"] == 0
    assert client.delete("/api/v1/devices/iphone-1").status_code == 404
