"""Create private task directories without cloning or executing source code."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from app.workspaces import store
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState
from app.workspaces.urls import validate_public_url


def workspace_root() -> Path:
    configured = os.environ.get("JARVIS_WORKSPACE_ROOT", "").strip() or (
        "~/.local/share/jarvis/workspaces"
    )
    root = Path(configured).expanduser()
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise WorkspaceError("workspace root must be a real directory, not a symlink")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, stat.S_IRWXU)
        return root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("workspace root is unavailable") from exc


def create_empty_workspace(
    *,
    mission_id: str,
    task_id: str,
    source_url: str = "",
    source_repo_id: str = "",
    source_revision: str = "",
) -> Workspace:
    """Reserve an isolated empty directory; repository intake is a later step."""
    if not mission_id.strip() or not task_id.strip():
        raise WorkspaceError("mission_id and task_id are required")
    if source_url:
        validate_public_url(source_url)
    root = workspace_root()
    workspace_id = uuid.uuid4().hex
    path = root / workspace_id
    if path.parent != root:
        raise WorkspaceError("workspace path escaped configured root")
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise WorkspaceError("workspace directory could not be created") from exc
    try:
        created = store.create(
            workspace_id=workspace_id,
            mission_id=mission_id,
            task_id=task_id,
            root_path=str(path),
            source_url=source_url,
            source_repo_id=source_repo_id,
            source_revision=source_revision,
            task_branch=f"codex/task-{workspace_id[:12]}",
            sandbox_runtime="UNCONFIGURED",
        )
    except Exception:
        # Do not leave an untracked directory if DB persistence fails.
        path.rmdir()
        raise
    # The directory is empty and durable. Reading it does not clone, install,
    # or run anything; those stay later, separately approved steps.
    return store.transition(created.workspace_id, WorkspaceState.READY)


def validate_workspace_path(workspace: Workspace, candidate: str) -> Path:
    """Resolve a path inside a workspace and reject traversal and symlinks."""
    configured_root = workspace_root()
    recorded_root = Path(workspace.root_path)
    if recorded_root.is_symlink():
        raise WorkspaceError("workspace root cannot be a symlink")
    try:
        root = recorded_root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("workspace root is unavailable") from exc
    if root.parent != configured_root or root.name != workspace.workspace_id:
        raise WorkspaceError("workspace is outside the configured workspace root")
    target = (root / candidate).resolve(strict=False)
    if target != root and root not in target.parents:
        raise WorkspaceError("path is outside this workspace")
    current = root
    for part in Path(candidate).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceError("parent traversal is not allowed")
        current = current / part
        if current.is_symlink():
            raise WorkspaceError("symlinks are not allowed in workspace paths")
    return target
