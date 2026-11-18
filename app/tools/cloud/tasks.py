"""Agent-facing task suggestions.

The handler has no status argument. Whatever else the model sends is
ignored, and the store still refuses an agent row that is not suggested.
Accepting and completing stay on the user API.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import Capability, Risk, ToolError, tool
from app.work_tasks.models import Actor, TaskError
from app.work_tasks import store


@tool(
    name="tasks.suggest",
    description=(
        "Suggest a personal task for the user. It is filed as suggested and "
        "does not become open until the user accepts it. Do not claim it was "
        "accepted or completed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the task is."},
            "notes": {"type": "string", "description": "Optional detail."},
            "due_at": {
                "type": "string",
                "description": "ISO-8601 due date or timestamp. A value with no zone is IST.",
            },
            "priority": {
                "type": "integer",
                "description": "1 (highest) to 3.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short labels.",
            },
            "recurrence": {
                "type": "string",
                "enum": ["none", "daily", "weekly", "monthly"],
            },
            "link_kind": {
                "type": "string",
                "enum": ["mission", "brand", "content_item"],
            },
            "link_id": {"type": "string"},
        },
        "required": ["title"],
    },
    capability=Capability.WRITE,
    risk=Risk.SAFE,
)
async def tasks_suggest(
    title: str,
    notes: str = "",
    due_at: str = "",
    priority: int = 2,
    tags: list[str] | None = None,
    recurrence: str = "none",
    link_kind: str = "",
    link_id: str = "",
    **_: Any,
) -> str:
    try:
        task = store.create(
            title,
            actor=Actor.AGENT,
            notes=notes,
            due_at=due_at or None,
            priority=priority,
            tags=tags,
            recurrence=recurrence or "none",
            link_kind=link_kind or None,
            link_id=link_id or None,
        )
    except TaskError as exc:
        raise ToolError(str(exc)) from exc
    return f"suggested {task.task_id}: {task.title} (status suggested, not accepted)"
