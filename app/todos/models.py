"""Todo domain model — a focus tool, not a list.

The problem this exists to solve is not "I forget things". It is "I have seven
open fronts and each one gets a fifth of an evening, so none of them finish."
Two decisions follow from that and both are load-bearing:

**Every todo belongs to a track.** Without a track a list of forty items looks
like one big backlog. With tracks it is visibly six backlogs wearing a trench
coat, which is the fact worth confronting.

**At most two tracks may be active at once** (`MAX_ACTIVE_TRACKS`). This is
enforced in the store, not suggested in the UI: activating a third is refused
until something is parked. Parked work stays visible and stays queryable — it
just stops competing for attention, and stops being offered by `next_todo`.

A conventional todo app would happily let all seven run. That is precisely the
behaviour being corrected, so it is the one thing this module will not do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Track(str, Enum):
    """A front of work. Deliberately a closed set.

    Free-text projects would recreate the problem: every stray idea becomes a
    new track and the WIP limit stops meaning anything. Adding one is a code
    change, which is the friction that keeps the list honest.
    """

    JOB_SWITCH = "job_switch"
    SALONI = "saloni"
    JARVIS = "jarvis"
    CRYPTO = "crypto"
    DOCS_TOOL = "docs_tool"
    CONTENT = "content"
    PERSONAL = "personal"


class TodoStatus(str, Enum):
    INBOX = "inbox"      # captured, not yet triaged
    NEXT = "next"        # triaged, ready to pick up
    DOING = "doing"      # started — finish before starting anything else
    BLOCKED = "blocked"  # waiting on someone or something external
    DONE = "done"


class Source(str, Enum):
    """Who filed this.

    Kept because agent-filed and self-filed todos age differently: a backlog
    full of agent suggestions nobody asked for is noise, and being able to see
    that split is what lets it be pruned.
    """

    ME = "me"
    CLAUDE = "claude"
    AGENT = "agent"


#: Statuses that no longer want attention.
CLOSED_STATUSES = frozenset({TodoStatus.DONE})

#: Statuses `next_todo` will never offer. Blocked work is waiting on someone
#: else; surfacing it as "what to do now" is how a list teaches you to ignore
#: it.
NOT_ACTIONABLE = frozenset({TodoStatus.DONE, TodoStatus.BLOCKED})

#: The whole point. See the module docstring.
MAX_ACTIVE_TRACKS = 2

#: 1 = highest. Three levels only — a ten-point scale just moves the
#: indecision from "what do I do" to "is this a 6 or a 7".
PRIORITY_MIN = 1
PRIORITY_MAX = 3


@dataclass
class Todo:
    todo_id: str
    title: str
    track: Track
    status: TodoStatus
    priority: int
    notes: str
    source: Source
    created_at: datetime | None = None
    updated_at: datetime | None = None
    due_at: datetime | None = None
    remind_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict:
        """JSON-safe shape. Used by the API, the MCP tools and the PWA alike."""
        return {
            "todo_id": self.todo_id,
            "title": self.title,
            "track": self.track.value,
            "status": self.status.value,
            "priority": self.priority,
            "notes": self.notes,
            "source": self.source.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "due_at": _iso(self.due_at),
            "remind_at": _iso(self.remind_at),
            "completed_at": _iso(self.completed_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def new_todo_id() -> str:
    return f"todo_{uuid.uuid4().hex[:12]}"


class TodoError(Exception):
    """Invalid input, or a refusal the caller is expected to act on.

    Carried to the API as 400 rather than 500 — a refused third active track is
    a correct outcome, not a fault.
    """
