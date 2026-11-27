"""Copy a repository into a task workspace. The source checkout is not modified."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from app.workspaces import store
from app.workspaces.access import record_event, require_grant
from app.workspaces.git import run_git
from app.workspaces.limits import measure_tree
from app.workspaces.manager import validate_workspace_path, workspace_root
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState
from app.workspaces.urls import validate_public_url

_SKIP = {"node_modules", ".venv", "venv", "__pycache__"}


def clone_argv(url: str, dest: Path) -> list[str]:
    """Arguments after the fixed git prefix. Credentials are not part of the command."""
    validate_public_url(url)
    return [
        # -c has to precede the subcommand. Git ignores it once clone has started.
        "-c", "http.followRedirects=false",
        "clone", "--depth", "1", "--single-branch", "--no-tags",
        "--no-recurse-submodules", "--config", "core.hooksPath=/dev/null",
        "--", url, str(dest),
    ]


def clone_credentials(url: str) -> tuple[dict[str, str], Callable[[], None]]:
    """Environment for a GitHub read token. The token is not placed in argv.

    Git reads it from the environment through an askpass helper. The helper
    file contains only the variable name. Redirects are disabled on the clone
    command so a response cannot retarget the request.
    """
    validate_public_url(url, resolve=True)
    token = os.environ.get("JARVIS_GITHUB_READ_TOKEN", "").strip()
    if not token:
        return {}, lambda: None
    if any(character in token for character in "\r\n\x00"):
        raise WorkspaceError("read token is invalid")
    directory = Path(tempfile.mkdtemp(prefix="jarvis-askpass-"))
    os.chmod(directory, stat.S_IRWXU)
    script = directory / "askpass"
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *[Uu]sername*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *) printf '%s\\n' \"$JARVIS_GITHUB_READ_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)

    def cleanup() -> None:
        shutil.rmtree(directory, ignore_errors=True)

    return {"GIT_ASKPASS": str(script), "JARVIS_GITHUB_READ_TOKEN": token}, cleanup


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
    url = validate_public_url(workspace.source_url, resolve=True)
    running = _begin(workspace)
    dest = validate_workspace_path(running, ".")
    try:
        extra_env, cleanup = clone_credentials(url)
        try:
            code, _out, err = run_git(
                clone_argv(url, dest), cwd=workspace_root(), timeout=180, extra_env=extra_env,
            )
        finally:
            cleanup()
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
