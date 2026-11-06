"""Durable personal tasks.

Every write commits before the function returns. A crash loses the request
in flight and not the row.

Agents do not get a back door around `suggested`. The check is here, not in
the route: a tool, a test, and a later caller all hit the same refusal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn
from app.work_tasks.models import (
    IST,
    NOTES_MAX,
    PRIORITY_MAX,
    PRIORITY_MIN,
    TAG_MAX,
    TAGS_MAX,
    TITLE_MAX,
    Actor,
    LinkKind,
    Recurrence,
    TaskError,
    TaskStatus,
    WorkTask,
    new_task_id,
)
from app.work_tasks.schedule import next_due


def init_db() -> None:
    t = ddl_types()
    # tags is TEXT holding a JSON array. A JSON column comes back as a dict
    # on Postgres and a string on SQLite, and that split has already broken
    # reads. A string is the same on both.
    script = """
    CREATE TABLE IF NOT EXISTS work_tasks (
        task_id      TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        notes        TEXT NOT NULL DEFAULT '',
        due_at       {ts},
        priority     INTEGER NOT NULL,
        status       TEXT NOT NULL,
        tags         TEXT NOT NULL DEFAULT '[]',
        recurrence   TEXT NOT NULL,
        series_id    TEXT NOT NULL,
        link_kind    TEXT,
        link_id      TEXT,
        source       TEXT NOT NULL,
        created_at   {ts},
        updated_at   {ts},
        completed_at {ts}
    );

    CREATE INDEX IF NOT EXISTS idx_work_tasks_status ON work_tasks (status, due_at);
    CREATE INDEX IF NOT EXISTS idx_work_tasks_series ON work_tasks (series_id, status);
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)


def create(
    title: str,
    *,
    actor: Actor | str,
    notes: str = "",
    due_at: Any = None,
    priority: int = 2,
    tags: list[str] | None = None,
    recurrence: str | Recurrence = Recurrence.NONE,
    link_kind: str | None = None,
    link_id: str | None = None,
    status: str | TaskStatus | None = None,
    series_id: str | None = None,
    source: Actor | str | None = None,
    now: datetime | None = None,
) -> WorkTask:
    who = _actor(actor)
    chosen = _status_for_create(who, status)
    stamp = _stamp(now)
    task_id = new_task_id()
    kind, linked = _link(link_kind, link_id)
    row_source = _actor(source) if source is not None else who
    with get_conn() as conn:
        execute(
            conn,
            """
            INSERT INTO work_tasks (
                task_id, title, notes, due_at, priority, status, tags, recurrence,
                series_id, link_kind, link_id, source, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                _title(title),
                _notes(notes),
                _store_due(due_at),
                _priority(priority),
                chosen.value,
                json.dumps(_tags(tags)),
                _recurrence(recurrence).value,
                series_id or task_id,
                kind,
                linked,
                row_source.value,
                stamp,
                stamp,
                None,
            ),
        )
    created = get(task_id)
    assert created is not None
    return created


def get(task_id: str) -> WorkTask | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM work_tasks WHERE task_id = ?", (task_id,))
    return _row(row) if row else None


def list_tasks(
    *,
    status: str | TaskStatus | None = None,
    tag: str | None = None,
    include_done: bool = False,
    limit: int = 200,
) -> list[WorkTask]:
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT * FROM work_tasks")
    items = [_row(row) for row in rows]
    if status:
        wanted = _coerce_status(status)
        items = [item for item in items if item.status is wanted]
    elif not include_done:
        items = [item for item in items if item.status is not TaskStatus.DONE]
    if tag:
        needle = tag.strip().lower()
        items = [item for item in items if needle in item.tags]
    far = datetime.max.replace(tzinfo=timezone.utc)
    items.sort(key=lambda item: (
        item.due_at is None,
        item.due_at or far,
        item.priority,
        item.created_at or far,
        item.task_id,
    ))
    return items[: max(1, limit)]


def update(task_id: str, **fields: Any) -> WorkTask | None:
    current = get(task_id)
    if current is None:
        return None
    if current.status is TaskStatus.DONE:
        raise TaskError("a completed task is frozen")
    if "status" in fields:
        raise TaskError("status changes go through accept or complete")
    title = _title(fields["title"]) if "title" in fields else current.title
    notes = _notes(fields["notes"]) if "notes" in fields else current.notes
    priority = _priority(fields["priority"]) if "priority" in fields else current.priority
    tags = _tags(fields["tags"]) if "tags" in fields else current.tags
    recurrence = _recurrence(fields["recurrence"]) if "recurrence" in fields else current.recurrence
    if "due_at" in fields:
        due = _store_due(fields["due_at"])
    else:
        due = _store_due(current.due_at)
    if "link_kind" in fields or "link_id" in fields:
        kind, linked = _link(
            fields.get("link_kind", current.link_kind),
            fields.get("link_id", current.link_id),
        )
    else:
        kind, linked = current.link_kind, current.link_id
    with get_conn() as conn:
        execute(
            conn,
            """
            UPDATE work_tasks
            SET title = ?, notes = ?, due_at = ?, priority = ?, tags = ?,
                recurrence = ?, link_kind = ?, link_id = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                title, notes, due, priority, json.dumps(list(tags)), recurrence.value,
                kind, linked, _stamp(None), task_id,
            ),
        )
    return get(task_id)


def delete(task_id: str) -> bool:
    with get_conn() as conn:
        existing = fetch_one(conn, "SELECT task_id FROM work_tasks WHERE task_id = ?", (task_id,))
        if existing is None:
            return False
        execute(conn, "DELETE FROM work_tasks WHERE task_id = ?", (task_id,))
    return True


def accept(task_id: str) -> WorkTask | None:
    current = get(task_id)
    if current is None:
        return None
    if current.status is not TaskStatus.SUGGESTED:
        raise TaskError("only a suggested task can be accepted")
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE work_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (TaskStatus.OPEN.value, _stamp(None), task_id),
        )
    return get(task_id)


def complete(task_id: str, *, now: datetime | None = None) -> tuple[WorkTask, WorkTask | None] | None:
    """Mark an open task done and, when it repeats, file the next occurrence.

    Both writes share one transaction. Suggested work is not completable:
    accepting it is the user's decision, and skipping that would let an agent
    put items straight onto the list.
    """
    current = get(task_id)
    if current is None:
        return None
    if current.status is TaskStatus.SUGGESTED:
        raise TaskError("accept the suggestion before completing it")
    if current.status is not TaskStatus.OPEN:
        raise TaskError("only an open task can be completed")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = _stamp(moment)
    following = next_due(current.due_at, current.recurrence, now=moment)
    with get_conn() as conn:
        execute(
            conn,
            """
            UPDATE work_tasks
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (TaskStatus.DONE.value, stamp, stamp, task_id),
        )
        spawned_id = _insert_next(conn, current, following, stamp) if following is not None else None
    done = get(task_id)
    assert done is not None
    spawned = get(spawned_id) if spawned_id else None
    return done, spawned


def _insert_next(conn: Any, current: WorkTask, due: datetime, stamp: str) -> str:
    """Insert the next open occurrence, or return one that already has this due."""
    existing = fetch_all(
        conn,
        "SELECT task_id, due_at FROM work_tasks WHERE series_id = ? AND status = ?",
        (current.series_id, TaskStatus.OPEN.value),
    )
    target = _store_due(due)
    for row in existing:
        if _store_due(_parse_ts(row["due_at"])) == target:
            return str(row["task_id"])
    task_id = new_task_id()
    execute(
        conn,
        """
        INSERT INTO work_tasks (
            task_id, title, notes, due_at, priority, status, tags, recurrence,
            series_id, link_kind, link_id, source, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            current.title,
            current.notes,
            target,
            current.priority,
            TaskStatus.OPEN.value,
            json.dumps(list(current.tags)),
            current.recurrence.value,
            current.series_id,
            current.link_kind,
            current.link_id,
            current.source.value,
            stamp,
            stamp,
            None,
        ),
    )
    return task_id


def _status_for_create(actor: Actor, status: str | TaskStatus | None) -> TaskStatus:
    if actor is Actor.AGENT:
        if status not in (None, "", TaskStatus.SUGGESTED, TaskStatus.SUGGESTED.value):
            raise TaskError("agents may only create suggested tasks")
        return TaskStatus.SUGGESTED
    if status in (None, "", TaskStatus.OPEN, TaskStatus.OPEN.value):
        return TaskStatus.OPEN
    if status in (TaskStatus.SUGGESTED, TaskStatus.SUGGESTED.value):
        raise TaskError("suggested tasks come from agents")
    raise TaskError("new tasks start open")


def _link(kind: Any, item_id: Any) -> tuple[str | None, str | None]:
    if kind in (None, "") and item_id in (None, ""):
        return None, None
    if kind in (None, "") or item_id in (None, ""):
        raise TaskError("a link needs both a kind and an id")
    try:
        parsed = kind if isinstance(kind, LinkKind) else LinkKind(str(kind).strip().lower())
    except ValueError as exc:
        known = ", ".join(item.value for item in LinkKind)
        raise TaskError(f"unknown link kind. Known: {known}") from exc
    linked = str(item_id).strip()
    if not linked or len(linked) > 80:
        raise TaskError("link id must be 1-80 characters")
    if parsed is LinkKind.MISSION:
        # Brand and content items have no table yet. A mission id that is not
        # in the store would be a made-up link, so that one is checked.
        from app.missions import store as mission_store
        if mission_store.get_mission(linked) is None:
            raise TaskError("unknown mission")
    return parsed.value, linked


def _title(value: str) -> str:
    title = str(value).strip()
    if not title:
        raise TaskError("title is required")
    if len(title) > TITLE_MAX:
        raise TaskError(f"title must be at most {TITLE_MAX} characters")
    return title


def _notes(value: str) -> str:
    notes = "" if value is None else str(value)
    if len(notes) > NOTES_MAX:
        raise TaskError(f"notes must be at most {NOTES_MAX} characters")
    return notes


def _priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError) as exc:
        raise TaskError("priority must be an integer 1-3") from exc
    if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise TaskError("priority must be 1-3 (1 = highest)")
    return priority


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise TaskError("tags must be a list of strings")
    cleaned: list[str] = []
    for part in parts:
        tag = str(part).strip().lower()
        if not tag:
            continue
        if len(tag) > TAG_MAX:
            raise TaskError(f"a tag must be at most {TAG_MAX} characters")
        if tag not in cleaned:
            cleaned.append(tag)
    if len(cleaned) > TAGS_MAX:
        raise TaskError(f"at most {TAGS_MAX} tags")
    return sorted(cleaned)


def _recurrence(value: str | Recurrence) -> Recurrence:
    if isinstance(value, Recurrence):
        return value
    try:
        return Recurrence(str(value).strip().lower() or Recurrence.NONE.value)
    except ValueError as exc:
        known = ", ".join(item.value for item in Recurrence)
        raise TaskError(f"unknown recurrence. Known: {known}") from exc


def _coerce_status(value: str | TaskStatus) -> TaskStatus:
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(item.value for item in TaskStatus)
        raise TaskError(f"unknown status. Known: {known}") from exc


def _actor(value: Actor | str) -> Actor:
    if isinstance(value, Actor):
        return value
    try:
        return Actor(str(value).strip().lower())
    except ValueError as exc:
        raise TaskError("actor must be user or agent") from exc


def _store_due(value: Any) -> str | None:
    parsed = _parse_due(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_due(value: Any) -> datetime | None:
    """Naive values are IST. A date with no time is 00:00 IST.

    The machine zone is not used. A UTC server would otherwise file an
    evening reminder against the wrong evening.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=IST)
        return parsed.astimezone(timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if len(text) == 10:
        try:
            parsed = datetime.fromisoformat(text).replace(tzinfo=IST)
        except ValueError as exc:
            raise TaskError("due_at must be an ISO-8601 date or timestamp") from exc
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TaskError("due_at must be an ISO-8601 date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def _stamp(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row(row: dict[str, Any]) -> WorkTask:
    raw_tags = row["tags"]
    if isinstance(raw_tags, str):
        try:
            parsed_tags = json.loads(raw_tags)
        except json.JSONDecodeError:
            parsed_tags = []
    else:
        parsed_tags = raw_tags or []
    return WorkTask(
        task_id=row["task_id"],
        title=row["title"],
        notes=row["notes"] or "",
        due_at=_parse_ts(row["due_at"]),
        priority=int(row["priority"]),
        status=TaskStatus(row["status"]),
        tags=tuple(parsed_tags),
        recurrence=Recurrence(row["recurrence"]),
        series_id=row["series_id"],
        link_kind=row["link_kind"],
        link_id=row["link_id"],
        source=Actor(row["source"]),
        created_at=_parse_ts(row["created_at"]),
        updated_at=_parse_ts(row["updated_at"]),
        completed_at=_parse_ts(row["completed_at"]),
    )
