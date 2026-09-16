"""Reminder delivery over Telegram.

Telegram rather than push notifications because the bot already exists and
works — a reminder system that ships tonight beats one that ships after an
APNs certificate. The `deliver` seam is deliberately narrow so a second
channel (web push, the PWA) slots in beside it later without touching the
scheduling logic.

Follows the project's "never fabricate" invariant: with no bot token
configured this reports itself unconfigured and sends nothing. It does **not**
mark todos as reminded in that case — silently consuming a reminder nobody
received is exactly the failure that makes people stop trusting the tool.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.todos import store
from app.todos.models import Todo

TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT = 10.0


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def configured() -> bool:
    return bool(_token() and _chat_id())


def format_reminder(todo: Todo) -> str:
    """One reminder, readable at a glance on a lock screen.

    Track is included because the same title means different things on
    different fronts, and the whole point of this system is knowing which front
    you are on.
    """
    priority_mark = {1: "!!", 2: "!", 3: ""}.get(todo.priority, "")
    lines = [f"{priority_mark} {todo.title}".strip(), f"track: {todo.track.value}"]
    if todo.due_at:
        lines.append(f"due: {todo.due_at.strftime('%d %b, %H:%M')}")
    if todo.notes:
        snippet = todo.notes if len(todo.notes) <= 220 else todo.notes[:217] + "..."
        lines.append("")
        lines.append(snippet)
    return "\n".join(lines)


async def deliver(text: str) -> bool:
    """Send one message. Returns whether Telegram accepted it."""
    if not configured():
        return False
    url = f"{TELEGRAM_API}/bot{_token()}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url, json={"chat_id": _chat_id(), "text": text, "disable_web_page_preview": True}
            )
        return response.status_code == 200
    except httpx.HTTPError:
        # A flaky network must not consume the reminder — leaving reminded_at
        # unset means the next sweep retries it.
        return False


async def sweep() -> dict[str, Any]:
    """Send every reminder that has come due. Safe to call on a timer.

    Each todo is marked reminded only after Telegram accepts it, so a crash or
    an outage mid-sweep costs a delayed reminder rather than a lost one.
    """
    if not configured():
        return {"status": "unconfigured", "sent": 0, "pending": len(store.due_reminders())}

    due = store.due_reminders()
    sent = 0
    failed = 0
    for todo in due:
        if await deliver(format_reminder(todo)):
            store.mark_reminded(todo.todo_id)
            sent += 1
        else:
            failed += 1

    return {"status": "ok", "sent": sent, "failed": failed, "due": len(due)}
