"""Durable mission state.

Every mission and task transition is written here immediately. Nothing
important lives only in memory, so killing the process loses in-flight latency
and nothing else -- which is what makes the 24/7 requirement achievable on a
host that restarts, sleeps, or gets redeployed under you.

Kept separate from `core/telemetry.py`: that module owns the fine-tuning
flywheel (what the model said and how good it was), this one owns operational
state (what is running and what happens next). They share a database but not a
purpose.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn
from app.missions.models import (
    ApprovalDecision,
    Budget,
    Mission,
    MissionStatus,
    Task,
    TaskStatus,
    new_approval_id,
    new_message_id,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


def _parse_ts(value: Any) -> datetime | None:
    """Parse a timestamp from either backend.

    SQLite returns the ISO string we wrote ("...T11:19:11.4+00:00"); Postgres
    returns a datetime whose str() uses a space instead of the "T". Comparing
    those as strings silently misjudges lease expiry, so both are normalised to
    aware datetimes here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def init_db() -> None:
    """Create mission tables. Safe to call repeatedly."""
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS missions (
        mission_id  TEXT PRIMARY KEY,
        session_id  TEXT,
        created_at  {ts},
        updated_at  {ts},
        goal        TEXT NOT NULL,
        status      TEXT NOT NULL,
        budget      {json},
        summary     TEXT DEFAULT '',
        error       TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS tasks (
        task_id           TEXT PRIMARY KEY,
        mission_id        TEXT NOT NULL,
        created_at        {ts},
        updated_at        {ts},
        agent_id          TEXT NOT NULL,
        objective         TEXT NOT NULL,
        status            TEXT NOT NULL,
        depth             INTEGER DEFAULT 0,
        depends_on        {json},
        result            TEXT DEFAULT '',
        error             TEXT DEFAULT '',
        attempts          INTEGER DEFAULT 0,
        idempotency_key   TEXT DEFAULT '',
        lease_expires_at  {ts},
        prompt_tokens     INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        tool_calls        INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS agent_messages (
        message_id     TEXT PRIMARY KEY,
        mission_id     TEXT NOT NULL,
        task_id        TEXT,
        created_at     {ts},
        sender         TEXT NOT NULL,
        receiver       TEXT NOT NULL,
        action         TEXT NOT NULL,
        payload        {json},
        status         TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS approvals (
        approval_id   TEXT PRIMARY KEY,
        mission_id    TEXT NOT NULL,
        task_id       TEXT NOT NULL,
        created_at    {ts},
        decided_at    {ts},
        agent_id      TEXT NOT NULL,
        tool_name     TEXT NOT NULL,
        tool_arguments {json},
        risk          TEXT NOT NULL,
        decision      TEXT NOT NULL,
        decided_by    TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_mission   ON tasks(mission_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_msg_mission     ON agent_messages(mission_id);
    CREATE INDEX IF NOT EXISTS idx_appr_decision   ON approvals(decision);
    """.format(**t)

    with get_conn() as conn:
        execute_script(conn, script)


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------


def _loads(value: Any, fallback: Any) -> Any:
    """JSON columns come back as str on SQLite and as objects on Postgres."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row_to_mission(row: dict[str, Any]) -> Mission:
    return Mission(
        mission_id=row["mission_id"],
        goal=row["goal"],
        status=MissionStatus(row["status"]),
        session_id=row["session_id"] or "",
        budget=Budget.from_dict(_loads(row["budget"], {})),
        summary=row["summary"] or "",
        error=row["error"] or "",
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        task_id=row["task_id"],
        mission_id=row["mission_id"],
        agent_id=row["agent_id"],
        objective=row["objective"],
        status=TaskStatus(row["status"]),
        depth=row["depth"] or 0,
        depends_on=_loads(row["depends_on"], []),
        result=row["result"] or "",
        error=row["error"] or "",
        attempts=row["attempts"] or 0,
        idempotency_key=row["idempotency_key"] or "",
        lease_expires_at=str(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        prompt_tokens=row["prompt_tokens"] or 0,
        completion_tokens=row["completion_tokens"] or 0,
        tool_calls=row["tool_calls"] or 0,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


# --------------------------------------------------------------------------
# Missions
# --------------------------------------------------------------------------


def create_mission(mission: Mission) -> Mission:
    stamp = _now()
    mission.created_at = mission.created_at or stamp
    mission.updated_at = stamp
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO missions (mission_id, session_id, created_at, updated_at,
                                  goal, status, budget, summary, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission.mission_id, mission.session_id, mission.created_at,
                mission.updated_at, mission.goal, mission.status.value,
                json.dumps(mission.budget.as_dict()), mission.summary, mission.error,
            ),
        )
    return mission


def update_mission(
    mission_id: str,
    *,
    status: MissionStatus | None = None,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now()]
    if status is not None:
        sets.append("status = ?")
        params.append(status.value)
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    params.append(mission_id)

    with get_conn() as conn:
        execute(conn, f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = ?", params)


def get_mission(mission_id: str) -> Mission | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM missions WHERE mission_id = ?", (mission_id,))
    return _row_to_mission(row) if row else None


def list_missions(limit: int = 25) -> list[Mission]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM missions ORDER BY created_at DESC, mission_id DESC LIMIT ?",
            (limit,),
        )
    return [_row_to_mission(row) for row in rows]


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def create_tasks(tasks: list[Task]) -> list[Task]:
    stamp = _now()
    with get_conn() as conn:
        for task in tasks:
            task.created_at = task.created_at or stamp
            task.updated_at = stamp
            execute(
                conn,
                """
                INSERT INTO tasks (task_id, mission_id, created_at, updated_at, agent_id,
                                   objective, status, depth, depends_on, result, error,
                                   attempts, idempotency_key, lease_expires_at,
                                   prompt_tokens, completion_tokens, tool_calls)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id, task.mission_id, task.created_at, task.updated_at,
                    task.agent_id, task.objective, task.status.value, task.depth,
                    json.dumps(task.depends_on), task.result, task.error, task.attempts,
                    task.idempotency_key, task.lease_expires_at, task.prompt_tokens,
                    task.completion_tokens, task.tool_calls,
                ),
            )
    return tasks


def update_task(
    task_id: str,
    *,
    status: TaskStatus | None = None,
    result: str | None = None,
    error: str | None = None,
    attempts: int | None = None,
    lease_expires_at: str | None = None,
    clear_lease: bool = False,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    tool_calls: int | None = None,
) -> None:
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now()]

    def add(column: str, value: Any) -> None:
        sets.append(f"{column} = ?")
        params.append(value)

    if status is not None:
        add("status", status.value)
    if result is not None:
        add("result", result)
    if error is not None:
        add("error", error)
    if attempts is not None:
        add("attempts", attempts)
    if clear_lease:
        add("lease_expires_at", None)
    elif lease_expires_at is not None:
        add("lease_expires_at", lease_expires_at)
    if prompt_tokens is not None:
        add("prompt_tokens", prompt_tokens)
    if completion_tokens is not None:
        add("completion_tokens", completion_tokens)
    if tool_calls is not None:
        add("tool_calls", tool_calls)

    params.append(task_id)
    with get_conn() as conn:
        execute(conn, f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", params)


def lease_task(task_id: str, seconds: int = 120) -> str:
    """Mark a task RUNNING with a time-bounded lease.

    A lease rather than a plain assignment: if the runner dies, the lease
    expires and reconciliation requeues the task instead of it hanging forever.
    """
    expires = _future(seconds)
    update_task(task_id, status=TaskStatus.RUNNING, lease_expires_at=expires)
    return expires


def get_task(task_id: str) -> Task | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    return _row_to_task(row) if row else None


def list_tasks(mission_id: str) -> list[Task]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM tasks WHERE mission_id = ? ORDER BY created_at ASC, task_id ASC",
            (mission_id,),
        )
    return [_row_to_task(row) for row in rows]


def find_task_by_idempotency_key(mission_id: str, key: str) -> Task | None:
    """Used to skip re-running work that already completed before a crash."""
    if not key:
        return None
    with get_conn() as conn:
        row = fetch_one(
            conn,
            """
            SELECT * FROM tasks
            WHERE mission_id = ? AND idempotency_key = ? AND status = ?
            """,
            (mission_id, key, TaskStatus.SUCCESS.value),
        )
    return _row_to_task(row) if row else None


# --------------------------------------------------------------------------
# Message bus (audit trail)
# --------------------------------------------------------------------------


def log_message(
    *,
    mission_id: str,
    sender: str,
    receiver: str,
    action: str,
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
    status: str = "",
) -> str:
    """Persist one bus message.

    Every dispatch and every result is recorded, so 'which agent asked what of
    whom' is answerable after the fact rather than only in a live log stream.
    """
    message_id = new_message_id()
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO agent_messages (message_id, mission_id, task_id, created_at,
                                        sender, receiver, action, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, mission_id, task_id, _now(), sender, receiver, action,
                json.dumps(payload or {}), status,
            ),
        )
    return message_id


def list_messages(mission_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT * FROM agent_messages WHERE mission_id = ?
            ORDER BY created_at ASC, message_id ASC LIMIT ?
            """,
            (mission_id, limit),
        )
    for row in rows:
        row["created_at"] = str(row["created_at"])
        row["payload"] = _loads(row["payload"], {})
    return rows


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------


def create_approval(
    *,
    mission_id: str,
    task_id: str,
    agent_id: str,
    tool_name: str,
    tool_arguments: dict[str, Any],
    risk: str,
) -> str:
    approval_id = new_approval_id()
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO approvals (approval_id, mission_id, task_id, created_at, decided_at,
                                   agent_id, tool_name, tool_arguments, risk, decision, decided_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id, mission_id, task_id, _now(), None, agent_id, tool_name,
                json.dumps(tool_arguments), risk, ApprovalDecision.PENDING.value, "",
            ),
        )
    return approval_id


def decide_approval(approval_id: str, decision: ApprovalDecision, decided_by: str = "user") -> bool:
    with get_conn() as conn:
        row = fetch_one(
            conn,
            "SELECT decision FROM approvals WHERE approval_id = ?",
            (approval_id,),
        )
        if not row or row["decision"] != ApprovalDecision.PENDING.value:
            return False   # unknown, or already decided -- never decide twice
        execute(
            conn,
            "UPDATE approvals SET decision = ?, decided_at = ?, decided_by = ? WHERE approval_id = ?",
            (decision.value, _now(), decided_by, approval_id),
        )
    return True


def get_approval(approval_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
    if row:
        row["tool_arguments"] = _loads(row["tool_arguments"], {})
        row["created_at"] = str(row["created_at"])
        row["decided_at"] = str(row["decided_at"]) if row["decided_at"] else None
    return row


def list_pending_approvals() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM approvals WHERE decision = ? ORDER BY created_at ASC",
            (ApprovalDecision.PENDING.value,),
        )
    for row in rows:
        row["tool_arguments"] = _loads(row["tool_arguments"], {})
        row["created_at"] = str(row["created_at"])
        row["decided_at"] = str(row["decided_at"]) if row["decided_at"] else None
    return rows


# --------------------------------------------------------------------------
# Boot reconciliation
# --------------------------------------------------------------------------


def reconcile_orphaned_tasks() -> dict[str, int]:
    """Recover state after a crash, restart, or redeploy.

    A task left RUNNING has no live runner: the process that owned it is gone.
    Expired-lease tasks are returned to READY so the scheduler picks them up;
    missions stuck mid-flight are moved back to RUNNING so they resume rather
    than hanging in a status nothing advances.

    Called once at startup, before serving traffic.
    """
    now = _now()
    now_dt = datetime.now(timezone.utc)
    requeued = 0
    resumed = 0

    with get_conn() as conn:
        orphans = fetch_all(
            conn,
            "SELECT task_id, lease_expires_at FROM tasks WHERE status = ?",
            (TaskStatus.RUNNING.value,),
        )
        for row in orphans:
            lease = _parse_ts(row["lease_expires_at"])
            # No lease, or one that has already lapsed -> nobody is working on it.
            if lease is None or lease <= now_dt:
                execute(
                    conn,
                    "UPDATE tasks SET status = ?, lease_expires_at = NULL, updated_at = ?"
                    " WHERE task_id = ?",
                    (TaskStatus.READY.value, now, row["task_id"]),
                )
                requeued += 1

        stalled = fetch_all(
            conn,
            "SELECT mission_id FROM missions WHERE status IN (?, ?)",
            (MissionStatus.RUNNING.value, MissionStatus.PLANNING.value),
        )
        for row in stalled:
            execute(
                conn,
                "UPDATE missions SET status = ?, updated_at = ? WHERE mission_id = ?",
                (MissionStatus.RUNNING.value, now, row["mission_id"]),
            )
            resumed += 1

    return {"tasks_requeued": requeued, "missions_resumed": resumed}
