"""Telegram, inbound: talk to Jarvis from the phone.

Outbound reminders already go through `app/todos/reminders.py`. This module is
the other direction — a long-poll loop that reads messages to the bot and
answers them.

Three choices worth knowing before changing anything here:

* **Long-polling, not a webhook.** `getUpdates` dials out, so it keeps D-014
  (nothing dials in) and works whether or not Funnel is up.

* **One bot per program (D-029).** Telegram hands each update to exactly one
  `getUpdates` caller. A second program polling the same token gets HTTP 409
  and the two steal each other's messages. Jarvis must not share the crypto
  engine's bot.

* **Only the owner's chat is answered.** There is no passcode header on
  Telegram, so the chat-ID check below *is* the gate. It lives here, once, not
  in each command.

Delivery is **at most once**. The update ID is recorded *before* the update is
handled, so a crash mid-handling loses that one message rather than replaying
it on restart. A lost message is visible — you sent something and got no
reply, so you send it again. A replayed `/add` is invisible — a duplicate todo
you discover a week later. Visible failure wins.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.db import ddl_types, execute, execute_script, fetch_one, get_conn
from app.todos import reminders
from app.todos import store as todo_store
from app.todos.models import TodoError, TodoStatus, Track

log = logging.getLogger("jarvis.telegram")

TELEGRAM_API = "https://api.telegram.org"

#: Seconds Telegram holds a getUpdates request open waiting for a message.
#: Long enough that an idle bot makes ~2 requests a minute, short enough to
#: sit under the HTTP client's timeout with room to spare.
POLL_SECONDS = 25

#: Telegram rejects messages over 4096 characters outright.
MAX_MESSAGE = 4000

CHANNEL = "telegram"

HELP = """Jarvis

/next — the one thing to do now
/add <track> <text> — file a todo (track optional)
/done <id> — close a todo (first few characters of the id are enough)
/tracks — what's active, what's parked
/help — this

Anything else is a conversation."""

#: (method, payload) -> parsed JSON body. Swapped out in tests.
Transport = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def configured() -> bool:
    return reminders.configured()


# --------------------------------------------------------------------------
# Durable state: the poll cursor and the conversation's session
# --------------------------------------------------------------------------


def init_db() -> None:
    t = ddl_types()
    script = """
    CREATE TABLE IF NOT EXISTS channel_state (
        channel        TEXT PRIMARY KEY,
        last_update_id INTEGER NOT NULL DEFAULT 0,
        session_id     TEXT DEFAULT '',
        updated_at     {ts}
    );
    """.format(**t)
    with get_conn() as conn:
        execute_script(conn, script)
        if fetch_one(conn, "SELECT channel FROM channel_state WHERE channel = ?", (CHANNEL,)) is None:
            execute(
                conn,
                "INSERT INTO channel_state (channel, last_update_id, session_id, updated_at)"
                " VALUES (?, 0, '', ?)",
                (CHANNEL, _now()),
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _state() -> dict[str, Any]:
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT * FROM channel_state WHERE channel = ?", (CHANNEL,))
    return row or {"last_update_id": 0, "session_id": ""}


def _record_update(update_id: int) -> None:
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE channel_state SET last_update_id = ?, updated_at = ? WHERE channel = ?",
            (update_id, _now(), CHANNEL),
        )


def _session_id() -> str:
    """One telemetry session for the Telegram conversation, kept across restarts.

    Persisted rather than created per message so the conversation's history is
    one thread in the training data, not hundreds of one-turn sessions.
    """
    from app.core.telemetry import telemetry

    existing = _state().get("session_id") or ""
    if existing and telemetry.session_exists(existing):
        return existing
    session_id = telemetry.start_session(client_type="TELEGRAM", user_agent="telegram-bot")
    with get_conn() as conn:
        execute(
            conn,
            "UPDATE channel_state SET session_id = ?, updated_at = ? WHERE channel = ?",
            (session_id, _now(), CHANNEL),
        )
    return session_id


# --------------------------------------------------------------------------
# Handling: pure functions of the message text, testable without a network
# --------------------------------------------------------------------------


def is_owner(chat_id: Any) -> bool:
    owner = reminders._chat_id()  # noqa: SLF001 - same env var, one source of truth
    return bool(owner) and str(chat_id) == owner


async def handle_update(update: dict[str, Any]) -> tuple[str, str] | None:
    """Turn one Telegram update into (chat_id, reply), or None to stay silent."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None  # edits, channel posts, button callbacks: not handled yet

    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not is_owner(chat_id):
        sender = (message.get("from") or {}).get("username") or "unknown"
        log.warning("ignored Telegram message from chat %s (@%s)", chat_id, sender)
        return None

    text = (message.get("text") or "").strip()
    if not text:
        return chat_id, "I can only read text for now."

    return chat_id, await handle_text(text)


async def handle_text(text: str) -> str:
    if not text.startswith("/"):
        return await _chat(text)

    command, _, rest = text.partition(" ")
    # "/next@JarvisBot" is what Telegram sends from a group or the command menu.
    command = command.split("@", 1)[0].lower()
    rest = rest.strip()

    if command in {"/start", "/help"}:
        return HELP
    if command == "/next":
        return _next()
    if command == "/add":
        return _add(rest)
    if command == "/done":
        return _done(rest)
    if command == "/tracks":
        return _tracks()
    return f"Unknown command {command}.\n\n{HELP}"


def _next() -> str:
    todo = todo_store.next_todo()
    if todo is None:
        return todo_store.track_summary()["verdict"]
    return f"{reminders.format_reminder(todo)}\n\nid: {_short(todo.todo_id)}"


def _add(rest: str) -> str:
    if not rest:
        return "Usage: /add <track> <text>"

    first, _, remainder = rest.partition(" ")
    tracks = {t.value for t in Track}
    if first.lower() in tracks and remainder.strip():
        track, title = first.lower(), remainder.strip()
    else:
        # Capture is never refused (see todos.store.add). Without a recognisable
        # track it lands in the personal inbox, to be triaged later.
        track, title = Track.PERSONAL.value, rest

    try:
        todo = todo_store.add(title, track, status=TodoStatus.INBOX)
    except TodoError as exc:
        return f"Couldn't file that: {exc}"
    return f"Filed to {todo.track.value}: {todo.title}\nid: {_short(todo.todo_id)}"


def _done(rest: str) -> str:
    needle = rest.strip().lower().removeprefix("todo_")
    if len(needle) < 4:
        return "Usage: /done <id> — at least the first 4 characters."

    matches = [
        t for t in todo_store.list_todos(limit=1000)
        if t.todo_id.removeprefix("todo_").startswith(needle)
    ]
    if not matches:
        return f"No open todo starts with {needle}."
    if len(matches) > 1:
        listed = "\n".join(f"{_short(t.todo_id)}  {t.title}" for t in matches[:5])
        return f"{len(matches)} todos match. Use more characters:\n{listed}"

    closed = todo_store.complete(matches[0].todo_id)
    return f"Done: {closed.title}" if closed else "That todo disappeared before I could close it."


def _tracks() -> str:
    summary = todo_store.track_summary()
    lines = [summary["verdict"], ""]
    for entry in summary["tracks"]:
        if entry["open"] == 0 and not entry["is_active"]:
            continue
        mark = "active" if entry["is_active"] else "parked"
        lines.append(f"{entry['track']}: {entry['open']} open ({mark})")
    return "\n".join(lines).strip()


async def _chat(text: str) -> str:
    # Imported here so this module can be loaded (and tested) without
    # building the engine router.
    from app.agent.orchestrator import orchestrator

    try:
        turn = await orchestrator.run_turn(session_id=_session_id(), user_prompt=text)
    except Exception as exc:  # noqa: BLE001 - the turn is already logged as failed
        return f"I couldn't answer that: {exc}"
    return turn.response


def _short(todo_id: str) -> str:
    return todo_id.removeprefix("todo_")[:6]


def split_message(text: str) -> list[str]:
    """Chunks under Telegram's limit, broken on line boundaries where possible."""
    if len(text) <= MAX_MESSAGE:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > MAX_MESSAGE:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_MESSAGE])
            line = line[MAX_MESSAGE:]
        if len(current) + len(line) > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# Transport + loop
# --------------------------------------------------------------------------


class ConflictError(RuntimeError):
    """HTTP 409: another program is polling this bot token."""


async def _http_transport(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{TELEGRAM_API}/bot{reminders._token()}/{method}"  # noqa: SLF001
    async with httpx.AsyncClient(timeout=POLL_SECONDS + 10) as client:
        response = await client.post(url, json=payload)
    if response.status_code == 409:
        raise ConflictError(response.text)
    response.raise_for_status()
    return response.json()


class TelegramChannel:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or _http_transport
        self._stop = asyncio.Event()

    async def poll_once(self, timeout: int = POLL_SECONDS) -> int:
        """Fetch and handle one batch of updates. Returns how many were handled."""
        last = int(_state().get("last_update_id") or 0)
        body = await self.transport(
            "getUpdates",
            {"offset": last + 1, "timeout": timeout, "allowed_updates": ["message"]},
        )
        handled = 0
        for update in body.get("result", []):
            update_id = int(update.get("update_id", 0))
            if update_id <= last:
                continue  # already recorded; Telegram re-sent it
            _record_update(update_id)  # before handling: at-most-once, see module doc
            last = update_id

            try:
                outcome = await handle_update(update)
            except Exception:  # noqa: BLE001 - one bad message must not stop the loop
                log.exception("failed to handle Telegram update %s", update_id)
                continue
            if outcome is None:
                continue
            chat_id, reply = outcome
            for chunk in split_message(reply):
                await self.transport(
                    "sendMessage",
                    {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                )
            handled += 1
        return handled

    async def run(self) -> None:
        """Poll until stopped. Network failures back off; they never end the loop."""
        delay = 2.0
        log.info("Telegram channel listening")
        while not self._stop.is_set():
            try:
                await self.poll_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except ConflictError:
                log.error(
                    "Telegram says another program is polling this bot token (HTTP 409). "
                    "Give Jarvis its own bot — see D-029. Retrying in 60s."
                )
                await self._sleep(60)
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram poll failed (%s); retrying in %.0fs", exc, delay)
                await self._sleep(delay)
                delay = min(delay * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
