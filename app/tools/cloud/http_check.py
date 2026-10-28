"""HTTP health check tool for watchers and agents."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.tools.registry import Capability, Risk, ToolError, tool

TIMEOUT = 15.0


def _evaluate(body: dict[str, Any], path: str) -> bool:
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return bool(node)


@tool(
    name="http.check",
    description=(
        "GET a URL and report whether it looks healthy. Checks status code and, "
        "optionally, a dotted JSON path (e.g. status for {\"status\":\"ok\"})."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
            "expect_status": {
                "type": "integer",
                "description": "HTTP status that counts as healthy.",
                "default": 200,
            },
            "expect_json_path": {
                "type": "string",
                "description": "Optional dotted path that must be truthy in a JSON body.",
                "default": "",
            },
        },
        "required": ["url"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def http_check(
    url: str,
    expect_status: int = 200,
    expect_json_path: str = "",
) -> str:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ToolError(f"request failed: {exc}") from exc

    ok = response.status_code == expect_status
    detail = f"status={response.status_code}"
    path = (expect_json_path or "").strip()
    if ok and path:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ToolError("response is not JSON") from exc
        ok = _evaluate(payload, path)
        detail = f"status={response.status_code}, path={path!r}={'ok' if ok else 'fail'}"

    return json.dumps({"ok": ok, "detail": detail, "url": url})
