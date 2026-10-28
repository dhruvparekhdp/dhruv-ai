"""Built-in schedule jobs."""

from __future__ import annotations

from typing import Any

from app.channels import telegram as telegram_channel
from app.todos import reminders as todo_reminders
from app.watchers.runner import run_watchers


async def run_reminders() -> dict[str, Any]:
    return await todo_reminders.sweep()


async def run_watcher_job() -> dict[str, Any]:
    result = await run_watchers()
    for alert in result.get("alerts", []):
        try:
            await telegram_channel.send_owner_text(alert)
        except Exception:  # noqa: BLE001
            pass
    return result
