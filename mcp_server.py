"""MCP server exposing the todo list to Claude.

A thin client over the HTTP API rather than a direct import of the store, for
one reason: this runs wherever Claude runs, and Jarvis runs on the host
laptop. Speaking HTTP means the same file works pointed at localhost during
development and at the Tailscale Funnel URL from anywhere else, with the
passcode doing the same job in both cases.

Five tools, and two of them are the point:

  * `todo_next` answers "what now" with exactly one item.
  * `track_summary` answers "how scattered am I", which is the question the
    whole system exists to make answerable.

The other three are plumbing so Claude can capture and close work without
leaving the conversation.

Run it:
    JARVIS_URL=http://127.0.0.1:8000 JARVIS_PASSCODE=dev python mcp_server.py

Register with Claude Code:
    claude mcp add jarvis-todos -- python /path/to/dhruv-ai/mcp_server.py
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

JARVIS_URL = os.getenv("JARVIS_URL", "http://127.0.0.1:8000").rstrip("/")
PASSCODE = os.getenv("JARVIS_PASSCODE", "")
TIMEOUT = 15.0

mcp = FastMCP("jarvis-todos")


def _headers() -> dict[str, str]:
    return {"X-Jarvis-Passcode": PASSCODE} if PASSCODE else {}


async def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """One HTTP call, with failures reported rather than raised.

    Per the project's never-fabricate invariant: a tool that cannot reach
    Jarvis says so plainly. Claude inventing a plausible todo list would be
    worse than no list at all, because it would be acted on.
    """
    url = f"{JARVIS_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        return {
            "error": f"Cannot reach Jarvis at {JARVIS_URL} ({exc.__class__.__name__}). "
            "Is the host laptop awake and the server running?"
        }

    if response.status_code == 401:
        return {"error": "Rejected by Jarvis: wrong or missing JARVIS_PASSCODE."}
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = response.text[:200]
        return {"error": detail or f"Jarvis returned HTTP {response.status_code}."}

    try:
        return response.json()
    except ValueError:
        return {"error": "Jarvis returned a non-JSON response."}


@mcp.tool()
async def todo_next() -> dict[str, Any]:
    """Get the single next thing to work on.

    Considers only active tracks, and prefers work already started over
    starting something new. Returns one item, not a list — if it returns
    nothing, the reason says why (usually: no active track).
    """
    return await _call("GET", "/api/v1/todos/next")


@mcp.tool()
async def track_summary() -> dict[str, Any]:
    """See how work is spread across tracks, and whether that spread is sane.

    Returns open counts per track, which tracks are active, and a plain verdict
    on whether attention is too thinly spread. Use this when asked what to
    focus on, or before agreeing to start something new.
    """
    return await _call("GET", "/api/v1/todos/summary")


@mcp.tool()
async def todo_add(
    title: str,
    track: str,
    priority: int = 2,
    notes: str = "",
    due_at: str = "",
    remind_at: str = "",
) -> dict[str, Any]:
    """File a todo.

    Args:
        title: What to do. Be specific — a vague todo gets re-decided every read.
        track: One of job_switch, saloni, jarvis, crypto, docs_tool, content, personal.
        priority: 1 (highest) to 3.
        notes: Context worth having when this surfaces later.
        due_at: ISO-8601 timestamp, optional.
        remind_at: ISO-8601 timestamp for a Telegram reminder, optional.

    Capture works on any track, active or parked — writing something down is
    never the thing to refuse.
    """
    payload: dict[str, Any] = {
        "title": title,
        "track": track,
        "priority": priority,
        "notes": notes,
        "source": "claude",
        "status": "next",
    }
    if due_at:
        payload["due_at"] = due_at
    if remind_at:
        payload["remind_at"] = remind_at
    return await _call("POST", "/api/v1/todos", json=payload)


@mcp.tool()
async def todo_list(
    track: str = "",
    status: str = "",
    active_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List todos.

    Args:
        track: Filter to one track. Empty means all.
        status: One of inbox, next, doing, blocked, done. Empty means all open.
        active_only: Only show work on currently active tracks.
        limit: Max items.
    """
    params: dict[str, Any] = {"limit": limit, "active_only": active_only}
    if track:
        params["track"] = track
    if status:
        params["status"] = status
    return await _call("GET", "/api/v1/todos", params=params)


@mcp.tool()
async def todo_update(
    todo_id: str,
    status: str = "",
    priority: int = 0,
    title: str = "",
    notes: str = "",
    due_at: str = "",
    remind_at: str = "",
) -> dict[str, Any]:
    """Update a todo. Set status to "done" to complete it.

    Only the fields you pass are changed. Set status to "doing" when starting
    something — `todo_next` then keeps offering it until it is finished, which
    is what stops a half-done task being abandoned for a new one.
    """
    payload: dict[str, Any] = {}
    if status:
        payload["status"] = status
    if priority:
        payload["priority"] = priority
    if title:
        payload["title"] = title
    if notes:
        payload["notes"] = notes
    if due_at:
        payload["due_at"] = due_at
    if remind_at:
        payload["remind_at"] = remind_at
    if not payload:
        return {"error": "Nothing to update — pass at least one field."}
    return await _call("PATCH", f"/api/v1/todos/{todo_id}", json=payload)


@mcp.tool()
async def set_track_active(track: str, is_active: bool, note: str = "") -> dict[str, Any]:
    """Activate or park a track. At most two may be active at once.

    Activating a third is refused, and the refusal names what is already
    active. That is deliberate: the limit is the product. When it refuses, ask
    which track to park rather than working around it.
    """
    return await _call(
        "POST",
        f"/api/v1/todos/tracks/{track}",
        json={"is_active": is_active, "note": note},
    )


if __name__ == "__main__":
    mcp.run()
