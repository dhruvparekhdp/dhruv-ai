"""Todo store and focus rules.

The CRUD tests are table stakes. The ones that matter are the focus rules —
the WIP limit and `next_todo`'s ordering — because those are the behaviour the
module exists for, and a regression there turns this back into an ordinary
todo list that lets you run seven things at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.todos import seed as todo_seed
from app.todos import store
from app.todos.models import MAX_ACTIVE_TRACKS, Source, TodoError, TodoStatus, Track


@pytest.fixture()
def db(env):  # noqa: ANN001 - `env` is the shared isolation fixture
    store.init_db()
    return store


def _iso(delta_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------


def test_add_and_get_roundtrip(db):
    todo = db.add("Ship the thing", Track.JARVIS, priority=1, notes="context")

    fetched = db.get(todo.todo_id)
    assert fetched is not None
    assert fetched.title == "Ship the thing"
    assert fetched.track is Track.JARVIS
    assert fetched.priority == 1
    assert fetched.status is TodoStatus.INBOX
    assert fetched.source is Source.ME


def test_blank_title_refused(db):
    with pytest.raises(TodoError):
        db.add("   ", Track.JARVIS)


def test_unknown_track_names_the_valid_ones(db):
    with pytest.raises(TodoError) as exc:
        db.add("x", "not_a_track")
    assert "job_switch" in str(exc.value)


def test_priority_out_of_range_refused(db):
    with pytest.raises(TodoError):
        db.add("x", Track.JARVIS, priority=9)


def test_completing_sets_completed_at_and_clearing_unsets_it(db):
    todo = db.add("x", Track.JARVIS)

    done = db.complete(todo.todo_id)
    assert done.status is TodoStatus.DONE
    assert done.completed_at is not None

    # Reopening must not leave a stale completion timestamp behind.
    reopened = db.update(todo.todo_id, status=TodoStatus.NEXT)
    assert reopened.completed_at is None


def test_update_refuses_unknown_field(db):
    todo = db.add("x", Track.JARVIS)
    with pytest.raises(TodoError):
        db.update(todo.todo_id, nonsense=True)


def test_list_hides_done_by_default(db):
    keep = db.add("open", Track.JARVIS)
    gone = db.add("closed", Track.JARVIS)
    db.complete(gone.todo_id)

    ids = {t.todo_id for t in db.list_todos()}
    assert keep.todo_id in ids
    assert gone.todo_id not in ids
    assert gone.todo_id in {t.todo_id for t in db.list_todos(include_done=True)}


# --------------------------------------------------------------------------
# The WIP limit — the actual product
# --------------------------------------------------------------------------


def test_third_active_track_is_refused(db):
    db.set_track_active(Track.JOB_SWITCH, True)
    db.set_track_active(Track.JARVIS, True)

    with pytest.raises(TodoError) as exc:
        db.set_track_active(Track.SALONI, True)

    message = str(exc.value)
    # The refusal has to name what is already active, or it forces a guess
    # instead of a decision.
    assert "job_switch" in message
    assert "jarvis" in message


def test_reactivating_an_already_active_track_is_not_a_third(db):
    db.set_track_active(Track.JOB_SWITCH, True)
    db.set_track_active(Track.JARVIS, True)

    result = db.set_track_active(Track.JARVIS, True, note="still on it")
    assert sorted(result["active_tracks"]) == ["jarvis", "job_switch"]


def test_parking_frees_a_slot(db):
    db.set_track_active(Track.JOB_SWITCH, True)
    db.set_track_active(Track.JARVIS, True)
    db.set_track_active(Track.JARVIS, False)

    result = db.set_track_active(Track.SALONI, True)
    assert "saloni" in result["active_tracks"]
    assert len(result["active_tracks"]) <= MAX_ACTIVE_TRACKS


def test_capture_is_never_blocked_by_the_limit(db):
    """Refusing to write something down sends it back into your head."""
    db.set_track_active(Track.JOB_SWITCH, True)
    db.set_track_active(Track.JARVIS, True)

    todo = db.add("idea on a parked track", Track.CRYPTO)
    assert todo.track is Track.CRYPTO


# --------------------------------------------------------------------------
# next_todo ordering
# --------------------------------------------------------------------------


def test_next_ignores_parked_tracks(db):
    db.set_track_active(Track.JARVIS, True)
    db.add("parked work", Track.SALONI, priority=1, status=TodoStatus.NEXT)
    wanted = db.add("active work", Track.JARVIS, priority=3, status=TodoStatus.NEXT)

    assert db.next_todo().todo_id == wanted.todo_id


def test_next_returns_none_when_nothing_is_active(db):
    db.add("work", Track.JARVIS, status=TodoStatus.NEXT)
    assert db.next_todo() is None


def test_doing_outranks_a_higher_priority_next(db):
    """Finishing beats starting — the anti-scatter rule, in one assertion."""
    db.set_track_active(Track.JARVIS, True)
    db.add("shiny new thing", Track.JARVIS, priority=1, status=TodoStatus.NEXT)
    started = db.add("half-finished", Track.JARVIS, priority=3, status=TodoStatus.DOING)

    assert db.next_todo().todo_id == started.todo_id


def test_overdue_beats_not_overdue_at_equal_status(db):
    db.set_track_active(Track.JARVIS, True)
    db.add("someday", Track.JARVIS, priority=1, status=TodoStatus.NEXT)
    overdue = db.add(
        "was due yesterday", Track.JARVIS, priority=1,
        status=TodoStatus.NEXT, due_at=_iso(-1440),
    )

    assert db.next_todo().todo_id == overdue.todo_id


def test_blocked_work_is_never_offered(db):
    db.set_track_active(Track.JARVIS, True)
    db.add("waiting on someone else", Track.JARVIS, priority=1, status=TodoStatus.BLOCKED)

    assert db.next_todo() is None


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------


def test_summary_flags_work_stranded_on_parked_tracks(db):
    db.set_track_active(Track.JARVIS, True)
    db.add("a", Track.JARVIS)
    db.add("b", Track.SALONI)
    db.add("c", Track.CRYPTO)

    summary = db.track_summary()
    assert summary["open_total"] == 3
    assert summary["tracks_with_open_work"] == 3
    assert "saloni" in summary["verdict"]
    assert "crypto" in summary["verdict"]


def test_summary_says_so_when_nothing_is_active(db):
    db.add("a", Track.JARVIS)
    assert "No active track" in db.track_summary()["verdict"]


def test_summary_lists_every_known_track_even_when_empty(db):
    names = {row["track"] for row in db.track_summary()["tracks"]}
    assert names == {t.value for t in Track}


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------


def test_due_reminders_only_returns_armed_and_unsent(db):
    past = db.add("ping me", Track.JARVIS, remind_at=_iso(-5))
    db.add("later", Track.JARVIS, remind_at=_iso(60))
    db.add("no reminder", Track.JARVIS)

    due = db.due_reminders()
    assert [t.todo_id for t in due] == [past.todo_id]


def test_marking_reminded_stops_it_repeating(db):
    todo = db.add("ping me", Track.JARVIS, remind_at=_iso(-5))
    db.mark_reminded(todo.todo_id)
    assert db.due_reminders() == []


def test_rescheduling_rearms_a_sent_reminder(db):
    todo = db.add("ping me", Track.JARVIS, remind_at=_iso(-5))
    db.mark_reminded(todo.todo_id)

    db.update(todo.todo_id, remind_at=_iso(-1))
    assert [t.todo_id for t in db.due_reminders()] == [todo.todo_id]


def test_completed_todos_stop_reminding(db):
    todo = db.add("ping me", Track.JARVIS, remind_at=_iso(-5))
    db.complete(todo.todo_id)
    assert db.due_reminders() == []


# --------------------------------------------------------------------------
# Seed
# --------------------------------------------------------------------------


def test_seed_populates_an_empty_table_and_sets_two_active_tracks(db):
    written = todo_seed.seed_if_empty()
    assert written > 0

    summary = db.track_summary()
    assert sorted(summary["active_tracks"]) == ["jarvis", "job_switch"]
    assert db.next_todo() is not None


def test_seed_is_idempotent(db):
    first = todo_seed.seed_if_empty()
    second = todo_seed.seed_if_empty()

    assert first > 0
    assert second == 0
    assert len(db.list_todos(limit=1000)) == first


def test_seed_does_not_resurrect_completed_work(db):
    todo_seed.seed_if_empty()
    for todo in db.list_todos(limit=1000):
        db.complete(todo.todo_id)

    assert todo_seed.seed_if_empty() == 0
    assert db.list_todos() == []
