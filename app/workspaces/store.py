"""Durable registry for task-scoped project workspaces."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.db import add_column_if_missing, execute, execute_script, fetch_all, fetch_one, get_conn
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState

_TRANSITIONS = {
    WorkspaceState.PREPARING: {WorkspaceState.READY, WorkspaceState.FAILED},
    WorkspaceState.READY: {
        WorkspaceState.RUNNING,
        WorkspaceState.PAUSED,
        WorkspaceState.FAILED,
        WorkspaceState.CLEANUP_PENDING,
    },
    WorkspaceState.RUNNING: {
        WorkspaceState.READY,
        WorkspaceState.PAUSED,
        WorkspaceState.FAILED,
        WorkspaceState.COMPLETE,
    },
    WorkspaceState.PAUSED: {
        WorkspaceState.READY,
        WorkspaceState.RUNNING,
        WorkspaceState.FAILED,
        WorkspaceState.CLEANUP_PENDING,
    },
    WorkspaceState.FAILED: {WorkspaceState.CLEANUP_PENDING},
    WorkspaceState.COMPLETE: {WorkspaceState.CLEANUP_PENDING},
    WorkspaceState.CLEANUP_PENDING: set(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def init_db() -> None:
    script = """
    CREATE TABLE IF NOT EXISTS project_workspaces (
        workspace_id TEXT PRIMARY KEY,
        mission_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        source_url TEXT NOT NULL DEFAULT '',
        source_repo_id TEXT NOT NULL DEFAULT '',
        source_revision TEXT NOT NULL DEFAULT '',
        task_branch TEXT NOT NULL DEFAULT '',
        sandbox_runtime TEXT NOT NULL DEFAULT 'UNCONFIGURED',
        cleanup_state TEXT NOT NULL DEFAULT 'RETAINED',
        root_path TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_project_workspaces_task
        ON project_workspaces(task_id, created_at);
    """
    with get_conn() as conn:
        execute_script(conn, script)
        add_column_if_missing(conn, "project_workspaces", "source_repo_id", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(conn, "project_workspaces", "task_branch", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(conn, "project_workspaces", "sandbox_runtime", "TEXT NOT NULL DEFAULT 'UNCONFIGURED'")
        add_column_if_missing(conn, "project_workspaces", "cleanup_state", "TEXT NOT NULL DEFAULT 'RETAINED'")


def _from_row(row: dict[str, object] | None) -> Workspace | None:
    if row is None:
        return None
    try:
        return Workspace(
            workspace_id=str(row["workspace_id"]),
            mission_id=str(row["mission_id"]),
            task_id=str(row["task_id"]),
            source_url=str(row["source_url"]),
            source_repo_id=str(row["source_repo_id"]),
            source_revision=str(row["source_revision"]),
            task_branch=str(row["task_branch"]),
            sandbox_runtime=str(row["sandbox_runtime"]),
            cleanup_state=str(row["cleanup_state"]),
            root_path=str(row["root_path"]),
            state=WorkspaceState(str(row["state"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            error_code=str(row["error_code"]),
        )
    except (KeyError, ValueError) as exc:
        raise WorkspaceError("stored workspace row is invalid") from exc


def create(
    *,
    workspace_id: str,
    mission_id: str,
    task_id: str,
    root_path: str,
    source_url: str = "",
    source_repo_id: str = "",
    source_revision: str = "",
    task_branch: str = "",
    sandbox_runtime: str = "UNCONFIGURED",
    cleanup_state: str = "RETAINED",
) -> Workspace:
    if not all((workspace_id, mission_id, task_id, root_path)):
        raise WorkspaceError("workspace, mission, task, and root identifiers are required")
    timestamp = _now()
    with get_conn() as conn:
        execute(
            conn,
            """INSERT INTO project_workspaces (
                workspace_id, mission_id, task_id, source_url, source_repo_id, source_revision,
                task_branch, sandbox_runtime, cleanup_state, root_path, state, created_at,
                updated_at, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                workspace_id, mission_id, task_id, source_url, source_repo_id, source_revision,
                task_branch, sandbox_runtime, cleanup_state, root_path,
                WorkspaceState.PREPARING.value, timestamp, timestamp, "",
            ),
        )
    result = get(workspace_id)
    if result is None:
        raise WorkspaceError("workspace was not persisted")
    return result


def get(workspace_id: str) -> Workspace | None:
    with get_conn() as conn:
        return _from_row(fetch_one(conn, "SELECT * FROM project_workspaces WHERE workspace_id = ?", (workspace_id,)))


def list_for_task(task_id: str) -> list[Workspace]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM project_workspaces WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        )
    return [workspace for row in rows if (workspace := _from_row(row)) is not None]


def record_intake(
    workspace_id: str,
    *,
    source_revision: str,
    sandbox_runtime: str,
) -> Workspace:
    """Store the revision that was copied in. The value is a git id, not a secret."""
    current = get(workspace_id)
    if current is None:
        raise WorkspaceError("workspace not found")
    revision = source_revision.strip()[:64]
    runtime = sandbox_runtime.strip()[:40] or current.sandbox_runtime
    with get_conn() as conn:
        execute(
            conn,
            """UPDATE project_workspaces
               SET source_revision = ?, sandbox_runtime = ?, updated_at = ?
               WHERE workspace_id = ?""",
            (revision, runtime, _now(), workspace_id),
        )
    result = get(workspace_id)
    if result is None:
        raise WorkspaceError("workspace was not persisted")
    return result


def transition(workspace_id: str, state: WorkspaceState, *, error_code: str = "") -> Workspace:
    current = get(workspace_id)
    if current is None:
        raise WorkspaceError("workspace not found")
    if state not in _TRANSITIONS[current.state]:
        raise WorkspaceError(f"invalid workspace transition: {current.state.value} -> {state.value}")
    with get_conn() as conn:
        execute(
            conn,
            """UPDATE project_workspaces
               SET state = ?, updated_at = ?, error_code = ?
               WHERE workspace_id = ? AND state = ?""",
            (state.value, _now(), error_code, workspace_id, current.state.value),
        )
    result = get(workspace_id)
    if result is None or result.state != state:
        raise WorkspaceError("workspace changed during transition")
    return result
