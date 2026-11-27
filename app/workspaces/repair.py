"""Run approved checks and stop when the same failure repeats.

A failed command stays failed. This loop does not invent a passing result or
edit the tree by itself.
"""

from __future__ import annotations

import hashlib

from app.workspaces import store
from app.workspaces.checks import run_check
from app.workspaces.models import Workspace, WorkspaceError


def run_approved_checks(
    workspace: Workspace,
    profiles: list[str],
    *,
    max_steps: int = 4,
    network: bool = False,
) -> dict[str, object]:
    if not profiles:
        raise WorkspaceError("no check profiles were requested")
    if not 1 <= max_steps <= 8:
        raise WorkspaceError("check step budget is outside the allowed bounds")
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    stopped = ""
    any_failed = False
    for profile in profiles[:max_steps]:
        loaded = store.get(workspace.workspace_id)
        if loaded is None:
            raise WorkspaceError("workspace not found")
        if loaded.state.value in {"PAUSED", "FAILED", "CLEANUP_PENDING"}:
            stopped = loaded.state.value
            break
        try:
            result = run_check(loaded, profile, network=network)
        except WorkspaceError as exc:
            results.append({
                "profile": profile,
                "passed": False,
                "exit_code": None,
                "output_untrusted": str(exc),
            })
            stopped = "sandbox_unavailable"
            break
        results.append(result)
        if result["passed"] is True:
            continue
        any_failed = True
        signature = hashlib.sha256(
            f"{profile}:{result['output_untrusted']}".encode()
        ).hexdigest()
        if signature in seen:
            stopped = "repeated_failure"
            break
        seen.add(signature)
    passed = bool(results) and not any_failed and not stopped
    return {
        "passed": passed,
        "stopped": stopped,
        "results": results,
        "fabricated": False,
    }
