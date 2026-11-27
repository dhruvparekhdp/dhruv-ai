"""Domain objects for disposable, task-scoped project workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkspaceState(StrEnum):
    PREPARING = "PREPARING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"
    CLEANUP_PENDING = "CLEANUP_PENDING"


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    mission_id: str
    task_id: str
    source_url: str
    source_repo_id: str
    source_revision: str
    task_branch: str
    sandbox_runtime: str
    cleanup_state: str
    root_path: str
    state: WorkspaceState
    created_at: str
    updated_at: str
    error_code: str = ""

    def describe(self) -> dict[str, str]:
        """Return user-visible metadata; never include credentials or env data."""
        return {
            "workspace_id": self.workspace_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "source_url": self.source_url,
            "source_repo_id": self.source_repo_id,
            "source_revision": self.source_revision,
            "task_branch": self.task_branch,
            "sandbox_runtime": self.sandbox_runtime,
            "cleanup_state": self.cleanup_state,
            "root_path": self.root_path,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
        }


class WorkspaceError(ValueError):
    """A workspace request violates lifecycle or path constraints."""

