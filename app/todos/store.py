"""Durable todo state, and the focus rules that make it useful.

Same durability contract as `missions/store.py` — every write lands
immediately, so a crash costs nothing. Shares the database, not the purpose:
missions are what Jarvis is doing, todos are what Dhruv is doing.

Two rules live here rather than in the API layer, because a rule enforced at
the edge is a suggestion:

  * `set_track_active` refuses a third active track (D: MAX_ACTIVE_TRACKS).
  * `next_todo` only ever looks inside active tracks.

Together those mean a parked track cannot leak back into attention by being
queried from a different entry point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.db import ddl_types, execute, execute_script, fetch_all, fetch_one, get_conn
from app.todos.models import (
    MAX_ACTIVE_TRACKS,
    NOT_ACTIONABLE,
    PRIORITY_MAX,
    PRIORITY_MIN,
    Source,
    Todo,
    TodoError,
    TodoStatus,
    Track,
    new_todo_id,
)

TITLE_MAX = 300
NOTES_MAX = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: Any) -> datetime | None:
    """Normalise both backends to aware datetimes.

    Identical reasoning to `missions/store._parse_ts`: SQLite hands back the
    ISO string we wrote, Postgres hands back a datetime whose str() uses a
    space instead of "T". Comparing those as strings silently misjudges what is
    overdue, which would quietly corrupt the one ordering this module exists to
    get right.
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
    """Create todo tables. Safe to call repeatedly."""
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS todos (
        todo_id      TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        track        TEXT NOT NULL,
        status       TEXT NOT NULL,
        priority     INTEGER NOT NULL DEFAULT 2,
        notes        TEXT DEFAULT '',
        source       TEXT NOT NULL DEFAULT 'me',
        created_at   {ts},
        updated_at   {ts},
        due_at       {ts},
        remind_at    {ts},
        reminded_at  {ts},
        completed_at {ts}
    );

    CREATE INDEX IF NOT EXISTS idx_todos_track_status ON todos (track, status);
    CREATE INDEX IF NOT EXISTS idx_todos_remind ON todos (remind_at);

    CREATE TABLE IF NOT EXISTS todo_tracks (
        track     TEXT PRIMARY KEY,
        is_active INTEGER NOT NULL DEFAULT 0,
        note      TEXT DEFAULT '',
        updated_at {ts}
    );
    """.format(**t)

    with get_conn() as conn:
        execute_script(conn, script)
        # Seed one row per known track so summaries list every front, including
        # the empty ones. A track with zero open items is information too.
        for track in Track:
            existing = fetch_one(
                conn, "SELECT track FROM todo_tracks WHERE track = ?", (track.value,)
            )
            if existing is None:
                execute(
                    conn,
                    "INSERT INTO todo_tracks (track, is_active, note, updated_at)"
                    " VALUES (?, ?, '', ?)",
                    (track.value, 0, _now()),
                )


# --------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------


def _coerce_track(value: str | Track) -> Track:
    if isinstance(value, Track):
        return value
    try:
        return Track(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(t.value for t in Track)
        raise TodoError(f"Unknown track '{value}'. Known tracks: {known}") from exc


def _coerce_status(value: str | TodoStatus) -> TodoStatus:
    if isinstance(value, TodoStatus):
        return value
    try:
        return TodoStatus(str(value).strip().lower())
    except ValueError as exc:
        known = ", ".join(s.value for s in TodoStatus)
        raise TodoError(f"Unknown status '{value}'. Known: {known}") from exc


def _coerce_priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError) as exc:
        raise TodoError("priority must be an integer 1-3.") from exc
    if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise TodoError(f"priority must be {PRIORITY_MIN}-{PRIORITY_MAX} (1 = highest).")
    return priority


def _coerce_ts(value: Any, field: str) -> str | None:
    """Accept an ISO string or datetime; store as ISO text."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="microseconds")
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError as exc:
        raise TodoError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="microseconds")


def _row_to_todo(row: dict[str, Any]) -> Todo:
    return Todo(
        todo_id=row["todo_id"],
        title=row["title"],
        track=Track(row["track"]),
        status=TodoStatus(row["status"]),
        priority=int(row["priority"]),
        notes=row.get("notes") or "",
        source=Source(row.get("source") or "me"),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
        due_at=_parse_ts(row.get("due_at")),
        remind_at=_parse_ts(row.get("remind_at")),
        completed_at=_parse_ts(row.get("completed_at")),
    )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def add(
    title: str,
    track: str | Track,
    *,
    priority: int = 2,
    status: str | TodoStatus = TodoStatus.INBOX,
    notes: str = "",
    due_at: Any = None,
    remind_at: Any = None,
    source: str | Source = Source.ME,
) -> Todo:
    """File a todo. Capture is always allowed, on any track, active or not.

    Deliberately *not* subject to the WIP limit: refusing to write something
    down is how it ends up in your head instead, which is the failure mode this
    whole module is arguing against. The limit governs attention, not capture.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise TodoError("title cannot be empty.")
    if len(clean_title) > TITLE_MAX:
        raise TodoError(f"title is longer than {TITLE_MAX} characters.")
    if len(notes or "") > NOTES_MAX:
        raise TodoError(f"notes are longer than {NOTES_MAX} characters.")

    todo = Todo(
        todo_id=new_todo_id(),
        title=clean_title,
        track=_coerce_track(track),
        status=_coerce_status(status),
        priority=_coerce_priority(priority),
        notes=(notes or "").strip(),
        source=source if isinstance(source, Source) else Source(str(source)),
    )
    now = _now()

    with get_conn() as conn:
        execute(
            conn,
            "INSERT INTO todos (todo_id, title, track, status, priority, notes, source,"
            " created_at, updated_at, due_at, remind_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                todo.todo_id,
                todo.title,
                todo.track.value,
                todo.status.value,
                todo.priority,
                todo.notes,
                todo.source.value,
                now,
                now,
                _coerce_ts(due_at, "due_at"),
                _coerce_ts(remind_at, "remind_at"),
            ),
        )
        row = fetch_one(conn, "SELECT * FROM todos WHERE todo_id = ?", (todo.todo_id,))
    return _row_to_todo(row)  # type: ignore[arg-type]


def update(todo_id: str, **fields: Any) -> Todo | None:
    """Patch a todo. Unknown keys are refused rather than silently dropped."""
    allowed = {"title", "track", "status", "priority", "notes", "due_at", "remind_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise TodoError(f"Cannot update {', '.join(sorted(unknown))}.")

    sets: list[str] = []
    params: list[Any] = []

    if "title" in fields:
        clean = (fields["title"] or "").strip()
        if not clean:
            raise TodoError("title cannot be empty.")
        sets.append("title = ?")
        params.append(clean[:TITLE_MAX])
    if "track" in fields:
        sets.append("track = ?")
        params.append(_coerce_track(fields["track"]).value)
    if "status" in fields:
        new_status = _coerce_status(fields["status"])
        sets.append("status = ?")
        params.append(new_status.value)
        # Completion time is derived from the status change rather than trusted
        # from the caller, so "done" always carries a truthful timestamp.
        if new_status is TodoStatus.DONE:
            sets.append("completed_at = ?")
            params.append(_now())
        else:
            sets.append("completed_at = NULL")
    if "priority" in fields:
        sets.append("priority = ?")
        params.append(_coerce_priority(fields["priority"]))
    if "notes" in fields:
        sets.append("notes = ?")
        params.append((fields["notes"] or "").strip()[:NOTES_MAX])
    for ts_field in ("due_at", "remind_at"):
        if ts_field in fields:
            sets.append(f"{ts_field} = ?")
            params.append(_coerce_ts(fields[ts_field], ts_field))
            if ts_field == "remind_at":
                # A rescheduled reminder has not been sent yet.
                sets.append("reminded_at = NULL")

    if not sets:
        return get(todo_id)

    sets.append("updated_at = ?")
    params.append(_now())
    params.append(todo_id)

    with get_conn() as conn:
        execute(conn, f"UPDATE todos SET {', '.join(sets)} WHERE todo_id = ?", params)
        row = fetch_one(conn, "SELECT * FROM todos WHERE todo_id = ?", (todo_id,))
    return _row_to_todo(row) if row else None


def complete(todo_id: str) -> Todo | None:
    return update(todo_id, status=TodoStatus.DONE)


def delete(todo_id: str) -> bool:
    with get_conn() as conn:
        existing = fetch_one(conn, "SELECT todo_id FROM todos WHERE todo_id = ?", (todo_id,))
        if existing is None:
            return False
        execute(conn, "DELETE FROM todos WHERE todo_id = ?", (todo_id,))
    return True


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get(todo_id: str) -> Todo | None:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM todos WHERE todo_id = ?", (todo_id,))
    return _row_to_todo(row) if row else None


def list_todos(
    *,
    track: str | Track | None = None,
    status: str | TodoStatus | None = None,
    include_done: bool = False,
    active_only: bool = False,
    due_before: Any = None,
    limit: int = 200,
) -> list[Todo]:
    where: list[str] = []
    params: list[Any] = []

    if track is not None:
        where.append("track = ?")
        params.append(_coerce_track(track).value)
    if status is not None:
        where.append("status = ?")
        params.append(_coerce_status(status).value)
    elif not include_done:
        where.append("status != ?")
        params.append(TodoStatus.DONE.value)
    if due_before is not None:
        where.append("due_at IS NOT NULL AND due_at <= ?")
        params.append(_coerce_ts(due_before, "due_before"))
    if active_only:
        where.append("track IN (SELECT track FROM todo_tracks WHERE is_active = 1)")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(int(limit), 1000)))

    with get_conn() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM todos {clause} ORDER BY priority ASC, created_at ASC LIMIT ?",
            params,
        )
    return [_row_to_todo(row) for row in rows]


def next_todo() -> Todo | None:
    """The single next thing to do, or None.

    Returns one item, never a list. A ranked list of ten is still a decision to
    make, and making that decision repeatedly is the tax this is meant to
    remove.

    Ordering, in priority order of the tiebreaks:

      1. `doing` first. Finishing something already started beats starting
         something new — that is the anti-scatter rule, and it is why `doing`
         outranks even an overdue `next`.
      2. Overdue before not-overdue.
      3. Explicit priority, then oldest.

    Only active tracks are considered. If everything is parked, the honest
    answer is None rather than reaching into parked work.
    """
    now = _now()
    excluded = [s.value for s in NOT_ACTIONABLE]
    placeholders = ", ".join("?" for _ in excluded)

    sql = f"""
        SELECT * FROM todos
        WHERE status NOT IN ({placeholders})
          AND track IN (SELECT track FROM todo_tracks WHERE is_active = 1)
        ORDER BY
            CASE status WHEN 'doing' THEN 0 WHEN 'next' THEN 1 ELSE 2 END,
            CASE WHEN due_at IS NOT NULL AND due_at <= ? THEN 0 ELSE 1 END,
            priority ASC,
            created_at ASC
        LIMIT 1
    """
    with get_conn() as conn:
        rows = fetch_all(conn, sql, [*excluded, now])
    return _row_to_todo(rows[0]) if rows else None


def track_summary() -> dict[str, Any]:
    """Open counts per track, plus an honest verdict on the spread.

    This is the report that makes the actual problem visible: not "40 things to
    do" but "40 things across 6 fronts, and you have decided all 6 matter".
    """
    with get_conn() as conn:
        counts = fetch_all(
            conn,
            "SELECT track, status, COUNT(*) AS n FROM todos"
            " WHERE status != ? GROUP BY track, status",
            (TodoStatus.DONE.value,),
        )
        track_rows = fetch_all(conn, "SELECT track, is_active, note FROM todo_tracks", ())
        done_row = fetch_one(
            conn, "SELECT COUNT(*) AS n FROM todos WHERE status = ?", (TodoStatus.DONE.value,)
        )

    by_track: dict[str, dict[str, Any]] = {
        row["track"]: {
            "track": row["track"],
            "is_active": bool(row["is_active"]),
            "note": row.get("note") or "",
            "open": 0,
            "by_status": {},
        }
        for row in track_rows
    }
    for row in counts:
        entry = by_track.setdefault(
            row["track"],
            {"track": row["track"], "is_active": False, "note": "", "open": 0, "by_status": {}},
        )
        entry["by_status"][row["status"]] = int(row["n"])
        entry["open"] += int(row["n"])

    active = sorted(name for name, e in by_track.items() if e["is_active"])
    open_total = sum(e["open"] for e in by_track.values())
    tracks_with_work = sorted(name for name, e in by_track.items() if e["open"] > 0)

    if len(active) > MAX_ACTIVE_TRACKS:
        verdict = (
            f"{len(active)} tracks active, limit is {MAX_ACTIVE_TRACKS}. "
            "Park one before starting anything new."
        )
    elif not active:
        verdict = "No active track. Nothing will be offered by next_todo until you activate one."
    elif len(tracks_with_work) > MAX_ACTIVE_TRACKS:
        parked = sorted(set(tracks_with_work) - set(active))
        verdict = (
            f"Focused on {', '.join(active)}. "
            f"{len(parked)} other track(s) hold open work and are parked: {', '.join(parked)}."
        )
    else:
        verdict = f"Focused on {', '.join(active)}."

    return {
        "active_tracks": active,
        "max_active": MAX_ACTIVE_TRACKS,
        "open_total": open_total,
        "done_total": int(done_row["n"]) if done_row else 0,
        "tracks_with_open_work": len(tracks_with_work),
        "verdict": verdict,
        "tracks": sorted(by_track.values(), key=lambda e: (-e["open"], e["track"])),
    }


# --------------------------------------------------------------------------
# Focus control
# --------------------------------------------------------------------------


def set_track_active(track: str | Track, active: bool, *, note: str = "") -> dict[str, Any]:
    """Activate or park a track. Refuses to exceed MAX_ACTIVE_TRACKS.

    The refusal is the feature. It names what is currently active so the caller
    has to make a real trade rather than quietly adding a third front.
    """
    resolved = _coerce_track(track)

    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT track FROM todo_tracks WHERE is_active = 1", ())
        current = {row["track"] for row in rows}

        if active and resolved.value not in current and len(current) >= MAX_ACTIVE_TRACKS:
            raise TodoError(
                f"Already at the limit of {MAX_ACTIVE_TRACKS} active tracks "
                f"({', '.join(sorted(current))}). Park one before activating "
                f"'{resolved.value}'."
            )

        execute(
            conn,
            "UPDATE todo_tracks SET is_active = ?, note = ?, updated_at = ? WHERE track = ?",
            (1 if active else 0, note.strip()[:500], _now(), resolved.value),
        )

    return {
        "track": resolved.value,
        "is_active": active,
        "active_tracks": sorted(
            (current | {resolved.value}) if active else (current - {resolved.value})
        ),
    }


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------


def due_reminders(now: Any = None) -> list[Todo]:
    """Todos whose reminder time has passed and which have not been sent yet.

    `reminded_at` is a separate column from `remind_at` on purpose: a restart
    partway through a send must not re-notify everything, and re-scheduling a
    reminder must arm it again. Clearing `reminded_at` on update is what makes
    the second case work.
    """
    cutoff = _coerce_ts(now, "now") or _now()
    with get_conn() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM todos"
            " WHERE remind_at IS NOT NULL AND remind_at <= ?"
            "   AND reminded_at IS NULL AND status != ?"
            " ORDER BY remind_at ASC LIMIT 50",
            (cutoff, TodoStatus.DONE.value),
        )
    return [_row_to_todo(row) for row in rows]


def mark_reminded(todo_id: str) -> None:
    with get_conn() as conn:
        execute(conn, "UPDATE todos SET reminded_at = ? WHERE todo_id = ?", (_now(), todo_id))


def stats() -> dict[str, int]:
    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT status, COUNT(*) AS n FROM todos GROUP BY status", ())
    return {row["status"]: int(row["n"]) for row in rows}
