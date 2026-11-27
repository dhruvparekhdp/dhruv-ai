"""Copy a repository into a task workspace. The source checkout is not modified."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from app.workspaces import store
from app.workspaces.access import record_event, require_grant
from app.workspaces.git import run_git
from app.workspaces.limits import measure_tree
from app.workspaces.manager import validate_workspace_path, workspace_root
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState

_SKIP = {"node_modules", ".venv", "venv", "__pycache__"}


def validate_public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise WorkspaceError("source_url is invalid") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise WorkspaceError("source_url must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise WorkspaceError("source_url cannot include query strings or fragments")
    return url


def clone_argv(url: str, dest: Path) -> list[str]:
    """Arguments after the fixed git prefix. The token never appears in the URL."""
    validate_public_url(url)
    extra: list[str] = []
    token = os.environ.get("JARVIS_GITHUB_READ_TOKEN", "").strip()
    host = urlsplit(url).hostname or ""
    if token and host.endswith("github.com"):
        extra = ["-c", "http.extraheader=AUTHORIZATION: bearer [REDACTED]"]
    return [
        "clone", "--depth", "1", "--single-branch", "--no-tags",
        "--no-recurse-submodules", "--config", "core.hooksPath=/dev/null",
        *extra,
        "--", url, str(dest),
    ]


def _clone_args(url: str, dest: Path) -> list[str]:
    """Same as clone_argv, but the live header is used only for the process."""
    argv = clone_argv(url, dest)
    token = os.environ.get("JARVIS_GITHUB_READ_TOKEN", "").strip()
    if token:
        return [
            part if part != "http.extraheader=AUTHORIZATION: bearer [REDACTED]"
            else f"http.extraheader=AUTHORIZATION: bearer {token}"
            for part in argv
        ]
    return argv


def _begin(workspace: Workspace) -> Workspace:
    if workspace.state is not WorkspaceState.READY:
        raise WorkspaceError("workspace is not ready for repository intake")
    require_grant(workspace, "repository_read")
    root = validate_workspace_path(workspace, ".")
    if any(root.iterdir()):
        raise WorkspaceError("workspace is not empty")
    return store.transition(workspace.workspace_id, WorkspaceState.RUNNING)


def _finish(workspace_id: str, *, revision: str) -> Workspace:
    store.record_intake(workspace_id, source_revision=revision, sandbox_runtime="UNCONFIGURED")
    record_event(
        workspace_id=workspace_id, actor="owner", tool="workspace.intake", profile="copy",
    )
    return store.transition(workspace_id, WorkspaceState.READY)


def _fail(workspace_id: str, code: str) -> None:
    current = store.get(workspace_id)
    if current is not None and current.state is WorkspaceState.RUNNING:
        store.transition(workspace_id, WorkspaceState.FAILED, error_code=code)


def intake_public(workspace: Workspace) -> Workspace:
    """Shallow-clone a public HTTPS repository into the empty task directory."""
    url = validate_public_url(workspace.source_url)
    running = _begin(workspace)
    dest = validate_workspace_path(running, ".")
    try:
        code, _out, err = run_git(_clone_args(url, dest), cwd=workspace_root(), timeout=180)
        if code != 0:
            raise WorkspaceError(err.strip() or "repository clone failed")
        hooks = dest / ".git" / "hooks"
        if hooks.is_dir() and not hooks.is_symlink():
            shutil.rmtree(hooks)
            hooks.mkdir(mode=0o700)
        measure_tree(dest)
        rev_code, revision, _err = run_git(["rev-parse", "HEAD"], cwd=dest)
        if rev_code != 0 or not revision.strip():
            raise WorkspaceError("cloned repository has no revision")
        branch = running.task_branch
        if branch:
            run_git(["checkout", "-B", branch], cwd=dest)
        return _finish(running.workspace_id, revision=revision.strip())
    except Exception:
        _fail(running.workspace_id, "intake_failed")
        raise


def _local_root() -> Path:
    raw = os.environ.get("JARVIS_LOCAL_REPOS", "").strip()
    if not raw:
        raise WorkspaceError("local repository intake is not configured")
    root = Path(raw).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceError("local repository root must be a real directory")
    return root.resolve(strict=True)


def intake_local(workspace: Workspace, source: str) -> Workspace:
    """Copy a granted local directory. Nothing is written back to the source."""
    granted = _local_root()
    candidate = Path(source).expanduser()
    if candidate.is_symlink():
        raise WorkspaceError("local source cannot be a symlink")
    try:
        src = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("local source is unavailable") from exc
    if src != granted and granted not in src.parents:
        raise WorkspaceError("local source is outside the granted directory")
    running = _begin(workspace)
    dest = validate_workspace_path(running, ".")
    try:
        _copy_tree(src, dest)
        hooks = dest / ".git" / "hooks"
        if hooks.is_dir() and not hooks.is_symlink():
            shutil.rmtree(hooks)
            hooks.mkdir(mode=0o700)
        measure_tree(dest)
        revision = ""
        if (dest / ".git").is_dir():
            code, revision, _err = run_git(["rev-parse", "HEAD"], cwd=dest)
            if code != 0:
                revision = ""
        return _finish(running.workspace_id, revision=revision.strip())
    except Exception:
        _fail(running.workspace_id, "intake_failed")
        raise


def _copy_tree(src: Path, dest: Path) -> None:
    for current, dirs, names in os.walk(src, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = [
            name for name in dirs
            if name not in _SKIP and not (current_path / name).is_symlink()
        ]
        relative = current_path.relative_to(src)
        target_dir = dest / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            origin = current_path / name
            if origin.is_symlink() or not origin.is_file():
                continue
            target = target_dir / name
            if target.is_symlink():
                raise WorkspaceError("refusing to write through a symlink")
            shutil.copy2(origin, target)
            # copy2 preserves mode. Drop anything executable that looks like a hook.
            if name.endswith((".sample",)) and "hooks" in target.parts:
                target.unlink()
