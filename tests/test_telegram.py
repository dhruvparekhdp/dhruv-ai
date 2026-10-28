"""Telegram inbound channel.

The tests that matter most are the gate (a stranger gets nothing, and changes
nothing) and the cursor (an update is handled at most once, even across a
restart). The commands are thin wrappers over the todo store, which has its
own suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels import telegram
from app.todos import store as todo_store
from app.todos.models import TodoStatus, Track

OWNER = "424242"
STRANGER = "999"


@pytest.fixture()
def channel_db(env, telemetry, monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    todo_store.init_db()
    telegram.init_db()


def _msg(text: str, chat: str = OWNER, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": int(chat)}, "from": {"username": "someone"}, "text": text},
    }


class FakeTelegram:
    """Records what the channel sends; serves a fixed batch of updates."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []
        self.offsets: list[int] = []

    async def __call__(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "getUpdates":
            self.offsets.append(payload["offset"])
            return {"ok": True, "result": list(self.updates)}
        if method == "sendMessage":
            self.sent.append(payload)
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


async def test_stranger_gets_no_reply_and_changes_nothing(channel_db):
    before = len(todo_store.list_todos())

    outcome = await telegram.handle_update(_msg("/add jarvis sneaky", chat=STRANGER))

    assert outcome is None
    assert len(todo_store.list_todos()) == before


async def test_unconfigured_owner_answers_nobody(channel_db, monkeypatch):
    # An empty TELEGRAM_CHAT_ID must not match an empty or missing chat id.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert await telegram.handle_update(_msg("/help")) is None


async def test_non_text_message_is_answered_honestly(channel_db):
    update = {"update_id": 1, "message": {"chat": {"id": int(OWNER)}, "photo": [{}]}}
    chat_id, reply = await telegram.handle_update(update)
    assert chat_id == OWNER
    assert "text" in reply


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def test_help_lists_commands(channel_db):
    reply = await telegram.handle_text("/help")
    for command in ("/next", "/add", "/done", "/tracks"):
        assert command in reply


async def test_command_suffix_from_bot_menu_is_accepted(channel_db):
    assert await telegram.handle_text("/help@JarvisBot") == await telegram.handle_text("/help")


async def test_add_with_a_track(channel_db):
    reply = await telegram.handle_text("/add jarvis wire the schedule")

    todos = todo_store.list_todos(track=Track.JARVIS)
    filed = [t for t in todos if t.title == "wire the schedule"]
    assert len(filed) == 1
    assert filed[0].status is TodoStatus.INBOX
    assert "Filed to jarvis" in reply


async def test_add_without_a_track_is_still_captured(channel_db):
    await telegram.handle_text("/add call the bank about the card")

    personal = todo_store.list_todos(track=Track.PERSONAL)
    assert any(t.title == "call the bank about the card" for t in personal)


async def test_add_with_nothing_explains_usage(channel_db):
    assert "Usage" in await telegram.handle_text("/add")


async def test_next_with_no_active_track_says_why(channel_db):
    reply = await telegram.handle_text("/next")
    assert "No active track" in reply


async def test_next_returns_the_one_item_with_a_short_id(channel_db):
    todo_store.set_track_active(Track.JARVIS, True)
    todo = todo_store.add("finish P1", Track.JARVIS, status=TodoStatus.NEXT, priority=1)

    reply = await telegram.handle_text("/next")

    assert "finish P1" in reply
    assert todo.todo_id.removeprefix("todo_")[:6] in reply


async def test_done_by_prefix(channel_db):
    todo = todo_store.add("close me", Track.JARVIS, status=TodoStatus.NEXT)
    prefix = todo.todo_id.removeprefix("todo_")[:6]

    reply = await telegram.handle_text(f"/done {prefix}")

    assert reply == "Done: close me"
    assert todo_store.get(todo.todo_id).status is TodoStatus.DONE


async def test_done_refuses_a_too_short_prefix(channel_db):
    assert "at least" in await telegram.handle_text("/done ab")


async def test_done_with_ambiguous_prefix_lists_matches(channel_db, monkeypatch):
    ids = iter(["todo_abcd11111111", "todo_abcd22222222"])
    monkeypatch.setattr("app.todos.store.new_todo_id", lambda: next(ids))
    todo_store.add("first", Track.JARVIS)
    todo_store.add("second", Track.JARVIS)

    reply = await telegram.handle_text("/done abcd")

    assert "2 todos match" in reply
    assert all(t.status is not TodoStatus.DONE for t in todo_store.list_todos())


async def test_tracks_reports_the_verdict(channel_db):
    todo_store.set_track_active(Track.JARVIS, True)
    todo_store.add("something", Track.JARVIS)

    reply = await telegram.handle_text("/tracks")

    assert "Focused on jarvis" in reply
    assert "jarvis: 1 open (active)" in reply


async def test_unknown_command_falls_back_to_help(channel_db):
    reply = await telegram.handle_text("/launch-rockets")
    assert reply.startswith("Unknown command /launch-rockets")


async def test_free_text_is_a_conversation_in_one_persistent_session(channel_db):
    first = await telegram.handle_text("hello jarvis")
    session = telegram._state()["session_id"]
    await telegram.handle_text("still there?")

    assert first  # the echo engine answers with no keys configured
    assert session.startswith("sess_")
    assert telegram._state()["session_id"] == session


# --------------------------------------------------------------------------
# The poll loop and its cursor
# --------------------------------------------------------------------------


async def test_poll_replies_and_advances_the_cursor(channel_db):
    fake = FakeTelegram([_msg("/help", update_id=10), _msg("/tracks", update_id=11)])
    channel = telegram.TelegramChannel(transport=fake)

    handled = await channel.poll_once(timeout=0)

    assert handled == 2
    assert [m["chat_id"] for m in fake.sent] == [OWNER, OWNER]
    assert telegram._state()["last_update_id"] == 11


async def test_an_update_is_handled_at_most_once_across_restarts(channel_db):
    updates = [_msg("/add jarvis only once", update_id=20)]

    await telegram.TelegramChannel(transport=FakeTelegram(updates)).poll_once(timeout=0)
    # A fresh channel object stands in for a process restart. Telegram re-sends
    # the same update because it was never confirmed with a later offset.
    second = FakeTelegram(updates)
    handled = await telegram.TelegramChannel(transport=second).poll_once(timeout=0)

    assert handled == 0
    assert second.offsets == [21]
    assert len([t for t in todo_store.list_todos() if t.title == "only once"]) == 1


async def test_stranger_updates_still_advance_the_cursor(channel_db):
    fake = FakeTelegram([_msg("/help", chat=STRANGER, update_id=30)])

    handled = await telegram.TelegramChannel(transport=fake).poll_once(timeout=0)

    assert handled == 0
    assert fake.sent == []
    assert telegram._state()["last_update_id"] == 30


async def test_a_failing_update_does_not_block_the_next(channel_db, monkeypatch):
    real = telegram.handle_text

    async def flaky(text: str) -> str:
        if text == "/boom":
            raise RuntimeError("bad handler")
        return await real(text)

    monkeypatch.setattr(telegram, "handle_text", flaky)
    fake = FakeTelegram([_msg("/boom", update_id=40), _msg("/help", update_id=41)])

    handled = await telegram.TelegramChannel(transport=fake).poll_once(timeout=0)

    assert handled == 1
    assert telegram._state()["last_update_id"] == 41


async def test_run_survives_errors_and_stops_cleanly(channel_db):
    calls = 0
    channel: telegram.TelegramChannel

    async def failing(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        channel.stop()
        raise RuntimeError("network down")

    channel = telegram.TelegramChannel(transport=failing)
    await channel.run()

    assert calls == 1


def test_long_replies_are_split_under_the_limit():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    chunks = telegram.split_message(text)

    assert len(chunks) > 1
    assert all(len(c) <= telegram.MAX_MESSAGE for c in chunks)
    assert "".join(chunks) == text


def test_one_giant_line_is_still_split():
    chunks = telegram.split_message("y" * (telegram.MAX_MESSAGE * 2 + 5))
    assert [len(c) for c in chunks] == [telegram.MAX_MESSAGE, telegram.MAX_MESSAGE, 5]
