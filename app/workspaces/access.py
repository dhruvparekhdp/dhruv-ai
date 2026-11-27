"""Mission-scoped grants and a redacted activity log.

A grant covers one category for one workspace. It does not authorize a push,
a pull request, a merge, a release, or a write to the original checkout.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.core.db import execute, execute_script, fetch_all, fetch_one, get_conn
from app.core.redact import redact
from app.workspaces.models import Workspace, WorkspaceError

CATEGORIES = (
    "repository_read",
    "workspace_write",
    "dependency_network",
    "build_test",
    "local_preview",
)
_DENIED = {
    "push", "pull_request", "merge", "release", "system", "secrets", "source_checkout",
}


def init_db() -> None:
    script = """
    CREATE TABLE IF NOT EXISTS project_workspace_grants (
        grant_id TEXT PRIMARY KEY,
        mission_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        category TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_project_workspace_grants
        ON project_workspace_grants(workspace_id, category);
    CREATE TABLE IF NOT EXISTS project_workspace_events (
        event_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        tool TEXT NOT NULL,
        grant_id TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '',
        exit_code INTEGER,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        paths TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """
    with get_conn() as conn:
        execute_script(conn, script)


def _now() -> datetime:
    return datetime.now(UTC)


def grant(
    workspace: Workspace,
    category: str,
    *,
    ttl_seconds: int = 3600,
) -> dict[str, str]:
    if category in _DENIED:
        raise WorkspaceError("that permission is never included in a task grant")
    if category not in CATEGORIES:
        raise WorkspaceError("unknown permission category")
    if not 60 <= ttl_seconds <= 24 * 60 * 60:
        raise WorkspaceError("grant lifetime is outside the allowed bounds")
    grant_id = uuid.uuid4().hex
    created = _now()
    expires = created + timedelta(seconds=ttl_seconds)
    with get_conn() as conn:
        execute(
            conn,
            """INSERT INTO project_workspace_grants (
                grant_id, mission_id, workspace_id, category, expires_at, revoked, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                grant_id, workspace.mission_id, workspace.workspace_id, category,
                expires.isoformat(timespec="seconds"), 0, created.isoformat(timespec="seconds"),
            ),
        )
    return {
        "grant_id": grant_id,
        "category": category,
        "expires_at": expires.isoformat(timespec="seconds"),
    }


def revoke(grant_id: str) -> bool:
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE project_workspace_grants SET revoked = 1 WHERE grant_id = ? AND revoked = 0",
            (grant_id,),
        )
        row = fetch_one(
            conn,
            "SELECT revoked FROM project_workspace_grants WHERE grant_id = ?",
            (grant_id,),
        )
    return bool(row and int(row["revoked"]) == 1)


def active_grant_id(workspace: Workspace, category: str) -> str:
    now = _now().isoformat(timespec="seconds")
    with get_conn() as conn:
        row = fetch_one(
            conn,
            """SELECT grant_id FROM project_workspace_grants
               WHERE workspace_id = ? AND mission_id = ? AND category = ?
                 AND revoked = 0 AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (workspace.workspace_id, workspace.mission_id, category, now),
        )
    if row is None:
        return ""
    return str(row["grant_id"])


def require_grant(workspace: Workspace, category: str) -> str:
    grant_id = active_grant_id(workspace, category)
    if not grant_id:
        raise WorkspaceError(f"this task has no active {category} grant")
    return grant_id


def list_grants(workspace_id: str) -> list[dict[str, str]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            """SELECT grant_id, category, expires_at, revoked, created_at
               FROM project_workspace_grants WHERE workspace_id = ?
               ORDER BY created_at DESC""",
            (workspace_id,),
        )
    return [
        {
            "grant_id": str(row["grant_id"]),
            "category": str(row["category"]),
            "expires_at": str(row["expires_at"]),
            "revoked": "yes" if int(row["revoked"]) else "no",
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def record_event(
    *,
    workspace_id: str,
    actor: str,
    tool: str,
    grant_id: str = "",
    profile: str = "",
    exit_code: int | None = None,
    duration_ms: int = 0,
    paths: list[str] | None = None,
) -> None:
    joined = redact(", ".join(paths or []))[:2_000]
    with get_conn() as conn:
        execute(
            conn,
            """INSERT INTO project_workspace_events (
                event_id, workspace_id, actor, tool, grant_id, profile, exit_code,
                duration_ms, paths, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex, workspace_id, actor[:80], tool[:80], grant_id,
                redact(profile)[:200], exit_code, max(0, duration_ms), joined,
                _now().isoformat(timespec="seconds"),
            ),
        )


def list_events(workspace_id: str) -> list[dict[str, str | int | None]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            """SELECT actor, tool, grant_id, profile, exit_code, duration_ms, paths, created_at
               FROM project_workspace_events WHERE workspace_id = ?
               ORDER BY created_at DESC LIMIT 50""",
            (workspace_id,),
        )
    return [
        {
            "actor": str(row["actor"]),
            "tool": str(row["tool"]),
            "grant_id": str(row["grant_id"]),
            "profile": str(row["profile"]),
            "exit_code": None if row["exit_code"] is None else int(row["exit_code"]),
            "duration_ms": int(row["duration_ms"]),
            "paths": str(row["paths"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
