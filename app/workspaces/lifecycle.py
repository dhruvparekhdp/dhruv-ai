"""Pause, resume, cancel, and confirmed cleanup. Files stay until cleanup is confirmed."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.workspaces import store
from app.workspaces.access import record_event
from app.workspaces.manager import validate_workspace_path
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState
from app.workspaces.repo import is_excluded_path


def pause(workspace: Workspace) -> Workspace:
    updated = store.transition(workspace.workspace_id, WorkspaceState.PAUSED)
    record_event(workspace_id=workspace.workspace_id, actor="owner", tool="workspace.pause")
    return updated


def resume(workspace: Workspace) -> Workspace:
    updated = store.transition(workspace.workspace_id, WorkspaceState.READY)
    record_event(workspace_id=workspace.workspace_id, actor="owner", tool="workspace.resume")
    return updated


def cancel(workspace: Workspace) -> Workspace:
    _stop_recorded_process(workspace)
    updated = store.transition(
        workspace.workspace_id, WorkspaceState.FAILED, error_code="cancelled",
    )
    record_event(workspace_id=workspace.workspace_id, actor="owner", tool="workspace.cancel")
    return updated


def cleanup_preview(workspace: Workspace) -> dict[str, object]:
    root = validate_workspace_path(workspace, ".")
    files = _files(root)
    return {
        "workspace_id": workspace.workspace_id,
        "file_count": len(files),
        "files": files[:50],
        "needs_confirmation": bool(files),
    }


def cleanup(workspace: Workspace, *, confirm: bool) -> dict[str, object]:
    preview = cleanup_preview(workspace)
    if preview["needs_confirmation"] and not confirm:
        raise WorkspaceError("cleanup needs confirmation because the workspace has files")
    root = validate_workspace_path(workspace, ".")
    for child in list(root.iterdir()):
        if child.is_symlink():
            child.unlink()
            continue
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    updated = workspace
    if workspace.state is not WorkspaceState.CLEANUP_PENDING:
        updated = store.transition(workspace.workspace_id, WorkspaceState.CLEANUP_PENDING)
    record_event(
        workspace_id=workspace.workspace_id,
        actor="owner",
        tool="workspace.cleanup",
        paths=[str(preview["file_count"])],
    )
    return {"workspace": updated.describe(), "removed_files": preview["file_count"]}


def _files(root: Path) -> list[str]:
    found: list[str] = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if is_excluded_path(relative):
                continue
            found.append(relative)
    return found


def _remove_tree(path: Path) -> None:
    for current, dirs, names in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in names:
            item = current_path / name
            if item.is_symlink() or item.is_file():
                item.unlink()
        for name in dirs:
            item = current_path / name
            if item.is_symlink():
                item.unlink()
            else:
                item.rmdir()
    path.rmdir()


def _stop_recorded_process(workspace: Workspace) -> None:
    """Kill a sandbox process only when its command line names this workspace."""
    pid_path = Path(workspace.root_path) / ".jarvis" / "pid"
    if not pid_path.is_file() or pid_path.is_symlink():
        return
    raw = pid_path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return
    pid = int(raw)
    command = _command(pid)
    if workspace.workspace_id not in command:
        return
    if not command.startswith(("podman ", "docker ")):
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def _command(pid: int) -> str:
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.is_file():
        return proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    ps = shutil.which("ps")
    if not ps:
        return ""
    try:
        done = subprocess.run(  # noqa: S603
            [ps, "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout
