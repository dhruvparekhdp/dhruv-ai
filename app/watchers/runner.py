"""Run enabled watchers; return alerts on state transitions."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.watchers import store

log = logging.getLogger("jarvis.watchers")

TIMEOUT = 15.0


def _evaluate(body: dict[str, Any], path: str) -> bool:
    if not path:
        return True
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return bool(node)


async def check_one(watcher: dict[str, Any]) -> tuple[bool, int | None, str]:
    url = watcher["url"]
    expect = int(watcher["expect_status"])
    path = (watcher.get("expect_json_path") or "").strip()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, None, str(exc)

    ok = response.status_code == expect
    detail = f"status={response.status_code}"
    if ok and path:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            ok = False
            detail = "expected JSON body"
        else:
            ok = _evaluate(payload, path)
            if not ok:
                detail = f"json path {path!r} failed"
    return ok, response.status_code, detail


async def run_watchers() -> dict[str, Any]:
    alerts: list[str] = []
    checked = 0
    for watcher in store.list_watchers():
        if not watcher.get("enabled"):
            continue
        checked += 1
        ok, status_code, detail = await check_one(watcher)
        name = watcher["name"]
        store.record_check(watcher_name=name, ok=ok, status_code=status_code, detail=detail)
        new_state = "up" if ok else "down"
        old_state = watcher.get("state") or "unknown"
        if old_state != new_state and old_state != "unknown":
            if new_state == "down":
                alerts.append(f"🔴 {name} is DOWN ({detail})")
            else:
                alerts.append(f"🟢 {name} is back UP")
        store.set_state(name, new_state)
    return {"status": "success", "checked": checked, "alerts": alerts}
