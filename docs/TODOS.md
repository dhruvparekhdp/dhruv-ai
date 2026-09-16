# Todos — the focus tool

Jarvis's todo module, and the MCP server that puts it inside Claude.

Missions are what Jarvis is doing. Todos are what **you** are doing. They share
a database and nothing else.

---

## Why it refuses things

An ordinary todo app will happily let you run seven fronts at once. That is the
behaviour this exists to correct, so two rules are enforced in the store rather
than suggested in a UI:

**At most two tracks may be active** (`MAX_ACTIVE_TRACKS`). Activating a third
is refused, and the refusal names what is already active so you make a real
trade instead of quietly adding a front.

**`todo_next` only looks inside active tracks**, and prefers work already
started (`doing`) over starting something new — even over a higher-priority
`next`. Finishing beats starting.

Capture is never refused. You can file a todo on any track, active or parked;
refusing to let you write something down just sends it back into your head.

### Tracks

`job_switch` · `saloni` · `jarvis` · `crypto` · `docs_tool` · `content` ·
`personal`

A closed set on purpose. Free-text projects would recreate the problem — every
stray idea becomes a new track and the limit stops meaning anything.

---

## API

All routes need `X-Jarvis-Passcode`, like everything else under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/todos` | List. Filters: `track`, `status`, `active_only`, `include_done` |
| `GET` | `/api/v1/todos/next` | **The one thing to do now.** Singular by design |
| `GET` | `/api/v1/todos/summary` | Open counts per track, plus a verdict on the spread |
| `POST` | `/api/v1/todos` | File one |
| `PATCH` | `/api/v1/todos/{id}` | Update. `status: "done"` completes it |
| `DELETE` | `/api/v1/todos/{id}` | Remove |
| `POST` | `/api/v1/todos/tracks/{track}` | Activate or park. **400 when at the limit** |
| `POST` | `/api/v1/todos/reminders/sweep` | Send due reminders. Idempotent |

The backlog seeds itself once, on a genuinely empty table, so the first
`next` answers with real work rather than an empty list.

---

## Connecting Claude

```bash
pip install -r requirements-mcp.txt

claude mcp add jarvis-todos \
  --env JARVIS_URL=https://<your-host> \
  --env JARVIS_PASSCODE=<passcode> \
  -- python /path/to/dhruv-ai/mcp_server.py
```

Point `JARVIS_URL` at `http://127.0.0.1:8000` locally, or the Tailscale Funnel
URL from anywhere else. The MCP server is a thin HTTP client, so the same file
works in both cases.

### Tools Claude gets

| Tool | What it answers |
|---|---|
| `todo_next` | "What should I do now?" — one item |
| `track_summary` | "How scattered am I?" — counts per track, and a verdict |
| `todo_add` | Capture, on any track |
| `todo_list` | Browse with filters |
| `todo_update` | Change status, priority, due date |
| `set_track_active` | Activate or park. Refuses a third |

If Jarvis is unreachable, every tool says so rather than inventing an answer —
a fabricated todo list would be acted on, which is worse than no list.

---

## Reminders

Telegram, because the bot already exists:

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

Set `remind_at` on a todo and call the sweep endpoint on a timer:

```bash
*/5 * * * * curl -fsS -X POST https://<host>/api/v1/todos/reminders/sweep \
  -H "X-Jarvis-Passcode: $JARVIS_PASSCODE" >/dev/null
```

With no token configured the sweep reports `unconfigured` and sends nothing —
and does **not** mark todos as reminded. Silently consuming a reminder nobody
received is how a tool loses your trust.

`reminded_at` is stored separately from `remind_at` so a restart mid-sweep
doesn't re-notify everything, and rescheduling a reminder arms it again.

---

## Tests

```bash
pytest tests/test_todos.py -q     # 26 tests, offline
```

The CRUD tests are table stakes. The ones that matter cover the WIP limit and
`next_todo`'s ordering — a regression there turns this back into an ordinary
list that lets you run seven things at once.
