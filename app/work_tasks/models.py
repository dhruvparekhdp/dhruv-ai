"""Task domain model.

The table is `work_tasks`, not `tasks`. `tasks` is already the mission graph,
and a second table of that name would be dropped by the test reset and would
collide with mission rows in production.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

PRIORITY_MIN = 1
PRIORITY_MAX = 3
TITLE_MAX = 300
NOTES_MAX = 4000
TAG_MAX = 32
TAGS_MAX = 20


class TaskStatus(str, Enum):
    SUGGESTED = "suggested"
    OPEN = "open"
    DONE = "done"


class Actor(str, Enum):
    USER = "user"
    AGENT = "agent"


class Recurrence(str, Enum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class LinkKind(str, Enum):
    MISSION = "mission"
    BRAND = "brand"
    CONTENT_ITEM = "content_item"


@dataclass
class WorkTask:
    task_id: str
    title: str
    notes: str
    due_at: datetime | None
    priority: int
    status: TaskStatus
    tags: tuple[str, ...]
    recurrence: Recurrence
    series_id: str
    link_kind: str | None
    link_id: str | None
    source: Actor
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict:
        due_ist = self.due_at.astimezone(IST) if self.due_at else None
        link = None
        if self.link_kind and self.link_id:
            link = {"kind": self.link_kind, "id": self.link_id}
        return {
            "task_id": self.task_id,
            "title": self.title,
            "notes": self.notes,
            "due_at": _iso(self.due_at),
            "due_at_ist": _iso(due_ist),
            "priority": self.priority,
            "status": self.status.value,
            "tags": list(self.tags),
            "recurrence": self.recurrence.value,
            "series_id": self.series_id,
            "link": link,
            "source": self.source.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "completed_at": _iso(self.completed_at),
        }


def new_task_id() -> str:
    # Not `task_`. That prefix belongs to mission steps.
    return f"wtsk_{uuid.uuid4().hex[:12]}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class TaskError(Exception):
    """Invalid input, or a refusal the caller is expected to act on."""
