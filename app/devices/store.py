"""Device check-ins: what a phone says about itself, kept with provenance.

Each check-in is a row, never an overwrite. "Battery is 40%" is useless alone;
"battery was 90% at 9 a.m. and 40% at noon" is the kind of signal later work
(the morning brief, noticing a bad day) actually needs.

Every report records `via` — what produced it: the app opening, a Siri
request, a Shortcuts automation — because a report sent on "arrived at the
office" means something a report sent on "app opened" does not.

All of it is deletable per device. Something that knows where your phone has
been must be something you can wipe.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, get_conn

#: Bounds on what a device may send. A phone app is a client like any other;
#: a bug in it must not be able to fill the disk.
MAX_METRICS = 64
MAX_METRICS_BYTES = 8_000


class DeviceError(ValueError):
    """The report was malformed. The message is safe to return to the client."""


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS device_reports (
        report_id   TEXT PRIMARY KEY,
        device_id   TEXT NOT NULL,
        name        TEXT DEFAULT '',
        platform    TEXT DEFAULT '',
        model       TEXT DEFAULT '',
        os_version  TEXT DEFAULT '',
        app_version TEXT DEFAULT '',
        via         TEXT DEFAULT '',
        metrics     TEXT DEFAULT '{{}}',
        created_at  {ts}
    );

    CREATE INDEX IF NOT EXISTS idx_device_reports_device ON device_reports (device_id, created_at);
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _clean_metrics(metrics: Any) -> dict[str, Any]:
    if metrics is None:
        return {}
    if not isinstance(metrics, dict):
        raise DeviceError("metrics must be an object")
    if len(metrics) > MAX_METRICS:
        raise DeviceError(f"at most {MAX_METRICS} metrics per report")
    for key, value in metrics.items():
        # Flat scalars only. Nested blobs are how an app ends up shipping a
        # whole HealthKit dump nobody asked for.
        if not isinstance(key, str) or not key or len(key) > 64:
            raise DeviceError("metric names must be non-empty strings up to 64 characters")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise DeviceError(f"metric '{key}' must be a string, number, boolean or null")
    encoded = json.dumps(metrics, sort_keys=True)
    if len(encoded) > MAX_METRICS_BYTES:
        raise DeviceError(f"metrics exceed {MAX_METRICS_BYTES} bytes")
    return metrics


def record(
    device_id: str,
    *,
    name: str = "",
    platform: str = "",
    model: str = "",
    os_version: str = "",
    app_version: str = "",
    via: str = "",
    metrics: Any = None,
) -> dict[str, Any]:
    device_id = (device_id or "").strip()
    if not device_id or len(device_id) > 100:
        raise DeviceError("device_id is required, up to 100 characters")
    cleaned = _clean_metrics(metrics)

    row = {
        "report_id": f"rep_{uuid.uuid4().hex[:16]}",
        "device_id": device_id,
        "name": name[:100],
        "platform": platform[:32],
        "model": model[:64],
        "os_version": os_version[:32],
        "app_version": app_version[:32],
        "via": via[:64],
        "created_at": _now(),
    }
    with get_conn() as conn:
        execute(
            conn,
            "INSERT INTO device_reports (report_id, device_id, name, platform, model,"
            " os_version, app_version, via, metrics, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["report_id"], row["device_id"], row["name"], row["platform"],
                row["model"], row["os_version"], row["app_version"], row["via"],
                json.dumps(cleaned, sort_keys=True), row["created_at"],
            ),
        )
    return {**row, "metrics": cleaned}


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    try:
        out["metrics"] = json.loads(out.get("metrics") or "{}")
    except (TypeError, ValueError):
        out["metrics"] = {}
    out["created_at"] = str(out["created_at"])
    return out


def history(device_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first. created_at is microsecond-precise (D-005), report_id breaks
    any remaining tie deterministically rather than leaving it to the planner."""
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM device_reports WHERE device_id = ?"
            " ORDER BY created_at DESC, report_id DESC LIMIT ?",
            (device_id, max(1, min(int(limit), 500))),
        )
    return [_row(r) for r in rows]


def latest_per_device() -> list[dict[str, Any]]:
    """The most recent report from every device, plus how many it has sent."""
    with get_conn() as conn:
        devices = fetch_all(
            conn,
            "SELECT device_id, COUNT(*) AS reports FROM device_reports GROUP BY device_id",
            (),
        )
    result = []
    for device in devices:
        latest = history(device["device_id"], limit=1)
        if latest:
            result.append({**latest[0], "reports": int(device["reports"])})
    result.sort(key=lambda r: r["created_at"], reverse=True)
    return result


def forget(device_id: str) -> int:
    """Delete every report from one device. Returns how many were removed."""
    with get_conn() as conn:
        count = fetch_all(
            conn, "SELECT COUNT(*) AS n FROM device_reports WHERE device_id = ?", (device_id,)
        )
        execute(conn, "DELETE FROM device_reports WHERE device_id = ?", (device_id,))
    return int(count[0]["n"]) if count else 0
