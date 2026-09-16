"""Mission and task domain model.

A Mission is one user goal. It owns a graph of Tasks, each assigned to exactly
one agent. The orchestrator is the only thing that creates tasks -- agents
never call each other, they return results and may *request* follow-up work
that the orchestrator is free to refuse.

Budgets live here rather than in the scheduler because they are part of the
mission's definition of done: a mission that exhausts its budget has failed in
a specific, reportable way rather than hanging.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MissionStatus(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"                # waiting on dependencies
    READY = "READY"                    # dependencies met, awaiting dispatch
    RUNNING = "RUNNING"                # leased to a runner
    NEEDS_APPROVAL = "NEEDS_APPROVAL"  # parked on a human decision
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"                # a dependency failed
    CANCELLED = "CANCELLED"


#: Statuses from which no further transition happens.
TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED}
)
TERMINAL_MISSION_STATUSES = frozenset(
    {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
)


class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


@dataclass(frozen=True)
class Budget:
    """Hard limits enforced before every dispatch.

    Without these a planning loop can burn an entire day of free-tier quota in
    minutes, so exhaustion is a first-class outcome rather than an exception.
    """

    max_tasks: int = 12
    max_depth: int = 3
    max_tokens: int = 120_000
    max_seconds: int = 300
    max_tool_calls: int = 40

    def as_dict(self) -> dict[str, int]:
        return {
            "max_tasks": self.max_tasks,
            "max_depth": self.max_depth,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Budget":
        if not raw:
            return cls()
        known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
        return cls(**known)


@dataclass
class Task:
    task_id: str
    mission_id: str
    agent_id: str
    objective: str
    status: TaskStatus = TaskStatus.PENDING
    depth: int = 0
    depends_on: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    attempts: int = 0
    # Guards against double-execution when a task is retried after a crash
    # mid-flight -- essential once tasks have side effects.
    idempotency_key: str = ""
    lease_expires_at: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Mission:
    mission_id: str
    goal: str
    status: MissionStatus = MissionStatus.PLANNING
    session_id: str = ""
    budget: Budget = field(default_factory=Budget)
    summary: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_MISSION_STATUSES


def new_mission_id() -> str:
    return f"msn_{uuid.uuid4()}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4()}"


def new_approval_id() -> str:
    return f"appr_{uuid.uuid4()}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4()}"
