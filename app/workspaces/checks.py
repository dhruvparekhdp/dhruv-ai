"""Allowlisted check profiles. The model names a profile; the server owns the argv."""

from __future__ import annotations

import os

from app.workspaces import store
from app.workspaces.access import record_event, require_grant
from app.workspaces.manager import validate_workspace_path
from app.workspaces.models import Workspace, WorkspaceError, WorkspaceState
from app.workspaces.sandbox import SandboxResult, run_container, setup_instructions

PROFILES: dict[str, list[str]] = {
    "python-pytest": ["python", "-m", "pytest", "-q"],
    "python-compile": ["python", "-m", "compileall", "-q", "."],
    "node-test": ["npm", "test"],
    "node-build": ["npm", "run", "build"],
    "rust-test": ["cargo", "test", "--offline"],
    "go-test": ["go", "test", "./..."],
    "make-test": ["make", "test"],
    "make-check": ["make", "check"],
}


def profile_argv(profile: str) -> list[str]:
    try:
        return list(PROFILES[profile])
    except KeyError as exc:
        raise WorkspaceError("unknown check profile") from exc


def run_check(
    workspace: Workspace,
    profile: str,
    *,
    actor: str = "owner",
    network: bool = False,
    preview: bool = False,
) -> dict[str, object]:
    """Run one profile inside the sandbox. Host execution is not a fallback."""
    argv = profile_argv(profile)
    grant_id = require_grant(workspace, "build_test")
    if network:
        require_grant(workspace, "dependency_network")
    if preview:
        require_grant(workspace, "local_preview")
    if workspace.state is not WorkspaceState.READY:
        raise WorkspaceError("workspace is not ready for a check")
    root = validate_workspace_path(workspace, ".")
    running = store.transition(workspace.workspace_id, WorkspaceState.RUNNING)
    timeout = _timeout()
    try:
        result = run_container(
            workspace=root,
            argv=argv,
            network=network,
            preview=preview,
            timeout=timeout,
        )
    except WorkspaceError:
        store.transition(running.workspace_id, WorkspaceState.READY, error_code="sandbox_unavailable")
        record_event(
            workspace_id=workspace.workspace_id,
            actor=actor,
            tool="workspace.run_check",
            grant_id=grant_id,
            profile=profile,
            exit_code=None,
        )
        raise
    store.transition(running.workspace_id, WorkspaceState.READY)
    record_event(
        workspace_id=workspace.workspace_id,
        actor=actor,
        tool="workspace.run_check",
        grant_id=grant_id,
        profile=profile,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
    )
    return _payload(profile, result)


def _timeout() -> int:
    raw = os.environ.get("JARVIS_SANDBOX_TIMEOUT_SECONDS", "1800")
    try:
        value = int(raw)
    except ValueError:
        return 1800
    return min(max(value, 1), 1800)


def _payload(profile: str, result: SandboxResult) -> dict[str, object]:
    return {
        "profile": profile,
        "exit_code": result.exit_code,
        "passed": result.exit_code == 0,
        "output_untrusted": result.output,
        "duration_ms": result.duration_ms,
        "runtime": result.runtime,
        "capped": result.capped,
        "setup": "" if result.runtime else setup_instructions(),
    }
