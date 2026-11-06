# AUD-1 — Baseline audit of dhruv-ai

Read-only audit of the Jarvis orchestrator in this repository. No runtime code was changed.

| | |
|---|---|
| Repository | `dhruv-ai` (this workspace). `crypto-signal-engine`, `NSE-TRADE`, and `ocr` are not present here. |
| Commit | `8ba215ed2449012f9f1173ae75ba4ece7efee913` (`main`, 16 Sep 2026 13:00 IST) |
| Audited | 6 Nov 2026, evening IST |
| Method | Read README, `CLAUDE.md`, `docs/`, route and security code, node agent, MCP server. Ran the test suite. Confirmed two behaviours with a local client (auth-off dispatch, shell allowlist). |

## Plan

Files read, then this document only:

- Docs: `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/SETUP-HOST.md`, `docs/SETUP-NODE.md`, `docs/TODOS.md`, `.env.example`
- Auth and HTTP: `app/core/config.py`, `app/core/security.py`, `app/main.py`, `app/static/app.js`
- Orchestration: `app/missions/scheduler.py`, `app/missions/store.py`, `app/missions/planner.py`, `app/missions/models.py`, `app/agents/runner.py`, `app/agents/registry.py`, `app/agents/definitions/*.yaml`
- Tools and nodes: `app/tools/registry.py`, `app/tools/cloud/*`, `app/tools/node/*`, `app/nodes/gateway.py`, `app/nodes/store.py`, `nodes/pc/agent.py`
- Memory, chat, MCP: `app/memory/*`, `app/agent/*`, `mcp_server.py`, `app/todos/reminders.py`

There is no `docs/audit/` before this file. There is no separate message-bus package: the “bus” is the `agent_messages` table.

## What exists

### Implemented in code

| Area | Where | What it actually does |
|---|---|---|
| Chat fast path | `app/agent/router.py`, `app/agent/orchestrator.py`, `app/agent/engines.py` | Keyword intent, one completion, Groq or Gemini or a local echo engine. No tools on this path. |
| Missions | `app/missions/scheduler.py`, `planner.py`, `store.py` | Goal → task graph, concurrent ready tasks, budgets checked before dispatch, state written on each transition. |
| Agents | `app/agents/definitions/{planner,research,memory,assistant,operator}.yaml`, `registry.py`, `runner.py` | Five YAML agents, one runner. Boot rejects unknown tools and tools the agent was not granted. |
| Tool permissions | `app/tools/registry.py` `check_permission` | Allowlist and capability both required. `DANGEROUS` sets `needs_approval`. The runner calls this before the handler. |
| Approvals | `scheduler.py` `_approval_hook`, `store.py` approvals table, `POST /api/v1/approvals/{id}`, PWA card | Agent path parks, polls the row, runs the tool only after `APPROVED`. Timeout denies. |
| Mission persistence | `app/missions/store.py` | `missions`, `tasks`, `agent_messages`, `approvals`. SQLite or Postgres via `app/core/db.py`. |
| Memory | `app/memory/store.py`, `search.py`, `embeddings.py`, `app/tools/cloud/memory.py` | Scoped rows with provenance, BM25 + optional local embeddings, Memory panel API. |
| Message audit | `store.log_message`, `GET /api/v1/missions/{id}/messages` | Persisted mission/task events. Not a broker, and not a per-tool log. |
| Nodes | `app/nodes/gateway.py`, `store.py`, `nodes/pc/agent.py`, `/ws/node` | Outbound WebSocket, hashed enrolment token and node secret, capability placement, path jail on the node. |
| MCP | `mcp_server.py` | HTTP client for todos only: next, summary, add, list, update, set track active. |
| Todos | `app/todos/*` | WIP limit, `next`, seed, Telegram send helper. |
| PWA | `app/static/` | Chat, mood, missions, approvals, memory, dataset export. No Files, System, Terminal, or Fleet panel. |

Twelve tools register at boot: `web.fetch`, `web.search`, `memory.search`, `memory.read`, `memory.write`, `memory.forget`, `fs.list`, `fs.read`, `fs.write`, `fs.delete`, `shell.exec`, `system.stats`.

`FINANCIAL` exists on the enum (`app/tools/registry.py` line 28) and is used by no tool.

### Documented, not implemented

| Claim | Where it is claimed | What the code does |
|---|---|---|
| Phase 3 screenshots, `fs.move`, AT-SPI, ydotool | `docs/ROADMAP.md` lines 76–86 | PC agent implements list/read/write/delete, `shell.exec`, `system.stats` only (`nodes/pc/agent.py` lines 208–215). |
| Files, System, Terminal, Fleet panels | `docs/ROADMAP.md` lines 88–92, `docs/SETUP-HOST.md` lines 263–267 | `app/static/index.html` has chat, mood, missions, approvals, memory. `app.js` never calls `/api/v1/nodes`. |
| Ansible, chrony playbook, firewall segmentation, backups, kill switch | `docs/ROADMAP.md` Phase 3 and 3.5 | No playbook or unit file in the repo. systemd snippets live in the setup docs only. |
| Devices dial in via `POST /api/v1/devices/checkin` | `README.md` lines 275–276, `docs/ROADMAP.md` lines 52–53 | No such route. |
| Telegram inline approvals and a reminder timer | `docs/DECISIONS.md` D-019, `README.md` lines 61–62 | `app/todos/reminders.py` can send one message. Nothing schedules `sweep`. No bot, no approval buttons. |
| iPhone `REPORT` node, Android tools | `docs/ARCHITECTURE.md` lines 68–70, 230–231 | Capability enum values exist. No iOS or Android agent. |
| `shell.sudo`, `android.tap`, screenshot tool | `docs/ARCHITECTURE.md` lines 59–60, 160–161 | Not registered. |
| Agents request follow-up work; `max_depth` refuses it | `docs/DECISIONS.md` D-007, `app/missions/models.py` lines 5–6 and 66 | No follow-up API. Planner sets `depth=0` (`planner.py` lines 67 and 167). `max_depth` is never read by the scheduler. |
| Boot resumes missions | `README.md` lines 35–36, `docs/ARCHITECTURE.md` lines 184–185, `app/main.py` lines 88–95 | Reconciliation rewrites rows. Nothing calls `scheduler.start` afterwards. |
| Heartbeat timeout reschedules work | `app/nodes/models.py` lines 37–39, `docs/ROADMAP.md` lines 71–72 | `stale_cutoff_seconds()` is never called. No sweeper. A dead node fails the in-flight dispatch; the task is not moved to another node. |
| `untrusted_ok` isolates model-generated code | `README.md` lines 46–47, `docs/ARCHITECTURE.md` lines 243–244 | `NodeGateway.select` honours the flag, but every tool leaves `untrusted=False`. The PC agent never reads `untrusted_ok`. |
| Idempotency stops a crashed task from running twice | `app/missions/models.py` lines 100–102 | Lookup only skips a *different* task whose key already `SUCCESS` (`scheduler.py` lines 242–250). The requeued task itself runs again. |
| SENSITIVE tools are “always audited” | `docs/ARCHITECTURE.md` line 160, `app/tools/registry.py` line 33 | No write to `tool_executions` from the runner. `log_tool_execution` is only called from tests. The message bus stores task dispatch and a 500-character result, not each tool call. |
| “No CORS” | `README.md` line 208, `docs/DECISIONS.md` D-003 | `CORSMiddleware` is installed with default origin `*` (`app/main.py` lines 126–131). |
| 150 tests, and `CLAUDE.md` “Next: Phase 3” | `README.md` line 106, `CLAUDE.md` lines 135–137 | This tree has the Phase 3 node code and **176** tests. |
| Trading, coding execution, voice, Ollama | Roadmap Phases 6–8, `app/agent/prompts.py` lines 36–39 | Chat intents `CODING_AGENT`, `QUANT_TRADING`, and `OS_AUTOMATION` are recognised and then refused (`router.py` lines 83–90). Missions can still assign `operator`, which has `shell.exec`. |

### Unverified

- Postgres suite. This run used SQLite only. `JARVIS_TEST_DATABASE_URL` was unset and no Postgres server was available. Dual-dialect behaviour is implemented and covered when that variable is set; it was not re-run here.
- A live PC node, Tailscale Funnel, or a real Groq/Gemini call. Engines were the echo fallback.
- Whether Funnel or journald actually retain the WebSocket query string. The client does put the passcode there (`app/static/app.js` line 540).
- Path-jail races (symlink swap between check and write). The resolve-then-check logic is present and the traversal tests pass.
- The pip yank notice for `google-genai==0.3.0` (Pillow CVEs). Not investigated past the installer warning.

## Subsystems

### MissionScheduler

`MissionScheduler` in `app/missions/scheduler.py` is the only dispatcher. Agents have no tool that calls another agent. `create_mission` inserts the row immediately. `start` schedules `_run_mission` on the event loop. The planner output is parsed defensively and falls back to one assistant task (`planner.py`).

The loop picks tasks whose dependencies `SUCCESS`, runs them with `asyncio.gather`, and checks time, tokens, tool calls, and task count before each wave (`scheduler.py` lines 161–170 and 191–202). Per-agent budgets are also enforced inside the runner (`runner.py` lines 106–116, `MAX_ITERATIONS = 8`).

`_running` is process memory. After a restart the dict is empty. `reconcile_orphaned_tasks` (`store.py` lines 501–547) only requeues `RUNNING` tasks with a missing or expired lease, and sets `RUNNING`/`PLANNING` missions back to `RUNNING`. It does not look at `NEEDS_APPROVAL`, and the lifespan never restarts those missions (`main.py` lines 88–95).

### Agents

Loaded from YAML at boot (`registry.py` lines 117–136). Roster injected into the planner excludes the planner itself.

| Agent | Tools | Capabilities |
|---|---|---|
| `planner` | none | none |
| `research` | `web.fetch`, `web.search`, `memory.search`, `memory.read` | READ |
| `memory` | search, read, write, forget | READ, WRITE |
| `assistant` | search, read | READ |
| `operator` | `fs.*`, `shell.exec`, `system.stats`, `memory.search` | READ, WRITE, EXECUTE |

### Tool permission check

`check_permission` (`app/tools/registry.py` lines 124–148) is real code, not a prompt:

1. Tool name must be on the agent allowlist.
2. Tool capability must be in the agent grants.
3. `Risk.DANGEROUS` returns `allowed=True, needs_approval=True`.
4. Otherwise it runs.

`schemas_for` hides tools the agent cannot see (lines 94–100). That is prompt-level defence in depth. The deny/approve decision is in `_invoke` (below).

### Is approval at the tool boundary?

**On the agent path, yes. On the direct node HTTP path, no.**

`AgentRunner._invoke` (`runner.py` lines 195–226) calls `check_permission` and, when approval is required, the hook. If the hook is missing it returns a denial and does not call the handler. The scheduler hook (`scheduler.py` lines 336–373) inserts an `approvals` row, sets the task and mission to `NEEDS_APPROVAL`, polls every 2 seconds for up to 600 seconds, and returns false on timeout (`decided_by="timeout"`). Only a true return reaches `tool.handler` or `gateway.dispatch`.

Tests cover that path for `memory.forget` (`tests/test_agents.py`) and the verdict for `shell.exec` / `fs.delete` (`tests/test_nodes.py` lines 406–418). They do not cover the HTTP exception.

`POST /api/v1/nodes/dispatch` (`main.py` lines 727–759) does not call `check_permission` and does not create an approval. The comment says that is deliberate because “the human is the one making this call.” The passcode is the only check. `shell.exec` and `fs.delete` are node tools, so they are eligible. With the passcode unset, that check is also skipped (finding F-01).

Human `DELETE /api/v1/memory/{id}` also skips the agent approval gate, on purpose (`main.py` lines 559–564, D-021).

### Mission persistence and the “message bus”

Tables are created in `app/missions/store.py`. Every mission create, plan, task dispatch, task result, and task failure calls `log_message`. That is the audited bus. It is a table read by one GET route. Tool arguments and outputs are not rows on that bus. `tool_executions` in telemetry is a schema waiting for a writer (`telemetry.py` lines 180–193).

Leases are 180 seconds (`scheduler.py` line 37). Approval can wait 600 seconds (line 39) without renewing the lease. Reconciliation does not run on a timer, only at boot, and only for status `RUNNING`.

### Memory

Writes record `source` (`agent:<id>` or `user`). Mission scope is partitioned by `scope_key` (`app/tools/cloud/memory.py` lines 23–25). `memory.read` requires a scope. `memory.search` with an empty scope does not pass a scope into `candidates`, and `candidates` then returns every scope, including other missions’ scratch rows (`store.py` lines 296–314). The tool text tells the model it may omit scope.

Embeddings are local ONNX via FastEmbed, optional. Search says when it fell back to keywords (`memory.py` lines 68–71). User delete does not require approval.

### Node protocol

Nodes connect to `/ws/node`. The socket is `accept()`ed before authentication (`main.py` lines 821–838). The first message must be `hello` within 15 seconds, with either an enrolment token or `node_id` + `secret`.

- Enrolment tokens: `secrets.token_urlsafe(24)`, stored as SHA-256, single use, 30-minute TTL (`app/nodes/store.py` lines 107–158, `models.py` lines 90–91).
- Node secret: `token_urlsafe(32)`, stored as SHA-256, compared with `hmac.compare_digest` of the hashes (`store.py` lines 222–228). Issued once in the `welcome` payload.
- A wrong `node_id` returns false before the compare, so existence is distinguishable by timing. The secret itself is 256 bits.
- Capabilities, RAM, and core count in `hello` are whatever the client sends (`main.py` lines 845–861) and are stored.
- Placement (`gateway.py` lines 90–114) prefers an idle connected node that advertises the capability, then the largest `ram_mb`.
- `dispatch` with an explicit `node_id` skips both the capability filter and `untrusted_ok` (lines 130–134).
- Request ids are `req_1`, `req_2`, … (lines 147–149). Any connected node may `resolve` any id (lines 190–197). The result is not checked against the node that was sent the dispatch.
- The PC agent reconnects with backoff from 2s to 60s (`nodes/pc/agent.py` lines 310–331). Credentials go to `nodes/pc/node.json` mode `0600`, gitignored.

The node does not re-check `DANGEROUS` or approval. It trusts the orchestrator.

### Remote shell and local execution

`shell.exec` is declared `DANGEROUS` and `requires_node` (`app/tools/node/system.py` lines 21–40). The body on the server raises if called in-process (`app/tools/node/_base.py`). Execution is `subprocess.run(command, shell=True, ...)` (`nodes/pc/agent.py` lines 173–186).

The allowlist, when non-empty, compares only `command.split()[0]`. Because the string is then a shell command, a later `;`, `&&`, or substitution still runs. Confirmed locally: allowlist `["echo"]` ran `echo hi; echo injected-marker` (both lines printed). `rm -rf /` as the first token was refused. An empty allowlist skips the check entirely (line 177). The CLI default is an empty list (lines 342–344), and the help text says that means allow anything.

The path jail resolves with `Path.resolve()` and then requires the path to be the root or a child (`agent.py` lines 124–129). Traversal tests pass. `fs.delete` refuses directories. `fs.write` creates parent directories inside the jail.

`system.stats` reads local `/proc` and disk usage. It does not run a shell.

There is no code path that shells out on the orchestrator host itself. Remote execution happens only on a connected node, and only if something calls `gateway.dispatch` (the runner after approval, or the HTTP route).

### MCP

`mcp_server.py` is a separate process (`requirements-mcp.txt`, not imported by `app/`). It forwards six todo tools to `/api/v1/todos*` with `X-Jarvis-Passcode` when `JARVIS_PASSCODE` is set. It cannot dispatch nodes, approve actions, or read memory. `JARVIS_URL` defaults to `http://127.0.0.1:8000`.

### Auth

**Unset `JARVIS_PASSCODE` disables auth.** `Settings.auth_enabled` is `bool(self.passcode)` (`config.py` lines 70–77). `check_passcode` returns `True` for every candidate, including a missing header (`security.py` lines 24–36). Whitespace-only is treated as unset (`_env` strips).

Confirmed with `TestClient` and no passcode in the environment: `GET /health` reported `auth_enabled: false`, `GET /api/v1/greeting` without a header returned 200, and `POST /api/v1/nodes/dispatch` of `shell.exec` returned **503** “no connected node”, not 401. The tool call was authorised; only placement failed. The log line was `auth=OFF`.

When the passcode is set, comparison is `hmac.compare_digest` (`security.py` line 36). Tests set a passcode and cover 401 on a bad header (`tests/test_api.py`). Nothing in the suite asserts the unset-passcode behaviour.

**Sessions are not logins.** `POST /api/v1/session/start` inserts `sess_<uuid4>` (`telemetry.py` lines 119–128). The PWA stores it in `localStorage` under `jarvis.session`. `GET /api/v1/session/{id}` only checks existence. There is no expiry, no revocation route, and no cookie. The passcode is the credential. The session id does not authorise anything by itself while auth is on. Mood history and memory list are global, not scoped to the session.

The passcode is stored in `localStorage` (`app.js` lines 14–35) and sent as `X-Jarvis-Passcode`. The log socket puts it in the query string (line 540) because browsers cannot set a WebSocket header (`main.py` lines 795–805).

**Unauthenticated even when a passcode is set**

| Route | Notes |
|---|---|
| `GET /health` | By design (D-004). Returns `auth_enabled`, storage backend, engine names, model ids, telemetry counts. No secrets. |
| `GET /docs`, `GET /redoc`, `GET /openapi.json` | FastAPI defaults. `openapi.json` returned 200 with 32 paths during the auth-off check; these routes are not behind `require_passcode` when auth is on either. |
| `GET /`, `/favicon.ico`, `/manifest.json`, `/sw.js`, `/static/*` | PWA shell. No secrets in the static files. |
| `WS /ws/node` | Not the user passcode. Enrolment token or node secret after `accept()`. |

Every `/api/v1/*` handler in `main.py` takes `Guarded` (`Depends(require_passcode)`). That dependency is a no-op when the passcode is unset.

There is no rate limit, lockout, or second factor.

## Environment, bind, CORS

Names only. Values were not printed.

Read by `get_settings()` (`app/core/config.py`):

`GROQ_API_KEY`, `GEMINI_API_KEY`, `GROQ_MODEL`, `GEMINI_MODEL`, `SEARCH_API_KEY`, `JARVIS_PASSCODE`, `DATABASE_URL`, `JARVIS_DB_PATH`, `JARVIS_APP_NAME`, `JARVIS_DEBUG`

Read elsewhere:

| Name | Where |
|---|---|
| `JARVIS_CORS_ORIGINS` | `app/main.py` line 128. Default `*`. Not in the README config table. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `app/todos/reminders.py`. Both required or nothing is sent. |
| `JARVIS_URL` | `mcp_server.py`. Default `http://127.0.0.1:8000`. |
| `JARVIS_TEST_DATABASE_URL` | `tests/conftest.py` only. |
| `DISPLAY`, `WAYLAND_DISPLAY` | PC agent capability detection, not secrets. |

`README.md` lines 144–153 omit `SEARCH_API_KEY`, `JARVIS_CORS_ORIGINS`, `JARVIS_APP_NAME`, the Telegram pair, and `JARVIS_URL`. `.env.example` includes the CORS variable and `SEARCH_API_KEY`.

**Bind.** The application never chooses a host. `uvicorn app.main:app --reload` in the README uses uvicorn’s loopback default. `docs/SETUP-HOST.md` line 217 runs `uvicorn app.main:app --host 0.0.0.0 --port 8000`, and line 169 publishes that port with `tailscale funnel 8000`. That is a public HTTPS origin whose only application gate is the shared passcode.

**CORS.** `CORSMiddleware` allows methods `*`, headers `*`, credentials, and origins from `JARVIS_CORS_ORIGINS` defaulting to `*` (`main.py` lines 124–131). On Starlette 0.41.3 (installed with FastAPI 0.115.6):

- Simple responses send `Access-Control-Allow-Origin: *` together with `Access-Control-Allow-Credentials: true` (`starlette/middleware/cors.py` lines 39–42).
- Preflight, because credentials are on, echoes the request `Origin` (lines 36 and 112–116) and mirrors requested headers because `allow_headers` is `*`.

Any browser origin can read unauthenticated responses. The app does not set cookies today, so this does not steal a session cookie. It does let a web page drive a passcode-less Jarvis, including one on localhost. It contradicts D-003.

## Tests

```
python3 -m pytest -q
176 passed, 1 warning in 7.86s
```

Warning: Starlette `TestClient` deprecation (`anyio.abc.BlockingPortal`), from `tests/test_api.py::test_health_is_open_and_reports_real_state`. Not a failure.

Collected 176 tests: `test_agents.py`, `test_api.py`, `test_memory.py`, `test_missions.py`, `test_nodes.py`, `test_router.py`, `test_security_invariants.py`, `test_telemetry.py`, `test_todos.py`.

`python3 -m compileall -q app nodes mcp_server.py` succeeded.

No ruff, mypy, or flake8 config is in the repo, and neither `ruff` nor `mypy` is installed. Type and lint checks were not run because there is nothing to run.

Postgres: **not run** (no server, variable unset).

Coverage the suite does not provide, which matters for the findings below:

- Auth disabled (`JARVIS_PASSCODE` empty).
- `POST /api/v1/nodes/dispatch` of a `DANGEROUS` tool.
- Shell metacharacters against a non-empty allowlist (the existing test only checks a bare `rm`).
- A `result` message from a node that did not receive the dispatch.
- Lifespan actually calling `scheduler.start` after reconciliation.
- `NEEDS_APPROVAL` surviving a restart.

## Findings

Line numbers refer to commit `8ba215e`.

### Critical

**F-01. Unset passcode plus the documented public bind is unauthenticated remote execution.**

`check_passcode` allows every caller when `JARVIS_PASSCODE` is empty (`app/core/security.py` lines 32–33). `POST /api/v1/nodes/dispatch` will then run a node tool with no approval (`app/main.py` lines 727–759). `shell.exec` is such a tool. The host recipe binds `0.0.0.0:8000` and funnels it (`docs/SETUP-HOST.md` lines 169 and 217). The node’s default allowlist is empty, which means every command (`nodes/pc/agent.py` lines 177 and 342–344).

Verified: with no passcode, dispatch of `shell.exec` was not rejected as unauthenticated. It reached placement and returned 503 only because no node was connected.

README line 146 states the unset behaviour on purpose (“Unset = no auth at all”) and `.env.example` says to leave it empty only on localhost. The setup doc does not refuse to funnel an instance that booted with auth off. `/health` does report `auth_enabled`, so the condition is visible if someone looks.

### High

**F-02. The shell allowlist does not constrain `shell=True`.**

`nodes/pc/agent.py` lines 173–186. First token is checked; the original string is executed by a shell. Verified: allowlist `echo` still ran a second command after `;`. `docs/SETUP-NODE.md` lines 70–75 describe the first-word check and show `rm` refused, which is true and incomplete. README lines 44–45 say the allowlist holds even if the orchestrator is compromised. It does not, once any allowlisted word can be the first token, and it does not exist at all when `--allow` is omitted.

**F-03. `DANGEROUS` is not enforced on the HTTP dispatch boundary.**

`app/main.py` lines 727–759. Invariant 3 in `CLAUDE.md` says dangerous tools always park, with no exception. The agent runner complies (`runner.py` lines 206–211). This route does not. One shared passcode, also stored in `localStorage` and in the WebSocket URL, is enough to delete files or run a shell on a connected node. `tests/test_nodes.py` lines 436–440 exercise the route with `system.stats` and never assert that `shell.exec` parks.

**F-04. Any connected node can complete any other node’s dispatch.**

`app/nodes/gateway.py` lines 147–149 mint guessable ids. Lines 190–197 accept a `result` from whichever socket sent it. A enrolled machine that loses placement can still answer `req_N` first. The real node may already be running the command. Placement trusts client-supplied `ram_mb` and capabilities (`main.py` lines 845–861, `gateway.py` lines 112–114), so a hostile node can also attract the work and receive file contents and command strings.

**F-05. The passcode is placed in the WebSocket URL on every log connection.**

`app/static/app.js` lines 5–7 and 540. `app/main.py` lines 795–805. Proxy, Funnel, and uvicorn access logs that record the request target will store the only credential. The REST API uses a header, which is the better of the two.

**F-06. Approval wait is longer than the lease, and a restart drops the waiter.**

Lease 180s, approval poll 600s (`scheduler.py` lines 37–39). Status during the wait is `NEEDS_APPROVAL` (lines 346–347), which reconciliation ignores (`store.py` lines 517–521). After a restart the in-memory poller is gone. `POST /api/v1/approvals/{id}` can still flip the row (`main.py` lines 493–501) and nothing executes it. Fail-closed for the tool, but the UI can show a decision that will never run. Missions that were `RUNNING` are not resumed either (`main.py` lines 88–95). That contradicts the crash-recovery claims in `README.md` lines 35–36 and `docs/ARCHITECTURE.md` lines 184–185.

### Medium

**F-07. Default CORS is every origin, with credentials enabled.**

`app/main.py` lines 126–131. See the CORS section above. Contradicts `README.md` line 208 and D-003. With auth off, a web page can call the API in the user’s browser, including a loopback dev server.

**F-08. `web.fetch` will request internal addresses.**

`app/tools/cloud/web.py` lines 52–63. Any `http` or `https` URL, redirects followed, 15s timeout. The research agent holds this tool at `SAFE` / `READ`. A mission (or an unauthenticated caller, if F-01 applies) can ask the host to fetch loopback, RFC1918, or a link-local address. No private-network block.

**F-09. `memory.search` without a scope reads every mission’s memory.**

`app/tools/cloud/memory.py` lines 50–56 and `app/memory/store.py` lines 307–314. D-009 says mission context does not leak. Explicit mission reads are partitioned. The default search is not.

**F-10. Node command timeout is the caller’s, and the orchestrator timeout does not kill the process.**

`shell_exec` uses the `timeout` argument with no upper bound (`nodes/pc/agent.py` line 173). The dispatch message includes `timeout` (`gateway.py` lines 154–161) and the agent ignores it (lines 289–298). The orchestrator stops waiting; the process keeps going. The HTTP schema caps its own wait at 300s (`main.py` line 233) and does not cap the argument the node honours.

**F-11. `untrusted_ok` is not on the execution path.**

No `@tool` sets `untrusted=True`. `gateway.dispatch` from the runner passes `tool.untrusted` (`runner.py` line 260), which is false. The PC agent will run `shell.exec` on a node that was never marked as a sandbox. The trust API (`main.py` lines 716–724) only flips a column.

**F-12. Passcode guessing is unbounded.**

`require_passcode` (`security.py` lines 39–47) has no delay, counter, or lockout. Comparison is constant-time per attempt. A weak passcode on a funnelled port is online-guessable.

### Low

**F-13. Public metadata.** `GET /health` (`main.py` lines 241–260) and `/openapi.json` are unauthenticated and describe the deployment. Counts and model names, not key material.

**F-14. Sessions do not expire.** `telemetry.py` lines 119–135. No delete. Low while the passcode gates the API. If auth is off, minting a session is enough to post chat.

**F-15. `max_depth` is unused.** `models.py` line 66. No spawn path today, so this is a false control rather than an open recursion bug. D-007 overclaims it.

**F-16. Tool audit table is unused by the runner.** `telemetry.py` lines 180–193. A sensitive write is not a durable tool row.

**F-17. Heartbeat expiry and `DRAINING` are unused.** `models.py` lines 34–39. `gateway.py` lines 214–215 only return the constant.

**F-18. Idempotency does not cover the task that crashed.** `models.py` lines 100–102 versus `scheduler.py` lines 242–250. A requeued shell or file write runs again. There is no boot resume today (F-06), so this fires if resume is added or if a second task shares the key.

**F-19. Node secret hash is unsalted SHA-256.** `store.py` lines 36–37. Acceptable for `token_urlsafe(32)`. Unknown ids return early (`store.py` lines 225–227).

**F-20. Pinned `google-genai==0.3.0` is yanked.** `requirements.txt` line 13. pip warned about Pillow CVEs. Not verified further. Out of scope to upgrade in this ticket.

**F-21. Chat still says shell and trading are unwired.** `app/agent/prompts.py` lines 36–39 and `router.py` lines 83–90. True for `POST /api/v1/command`. False for a mission that the planner assigns to `operator`.

**F-22. Reminder “timer” is an HTTP call.** `main.py` lines 675–678. Unconfigured Telegram does not mark todos reminded (`reminders.py` lines 76–83). That part matches the README. The timer does not exist.

## Docs that contradict the code

| Document | Says | Code |
|---|---|---|
| `CLAUDE.md` lines 135–137 | 150 tests. Next work is Phase 3. | Node protocol and PC agent are in tree. 176 tests pass on SQLite. |
| `README.md` lines 35–36, 44–47, 106, 146, 208, 259–266 | Crash recovery resumes missions. Shell allowlist holds if the orchestrator is compromised. 150 tests. Unset passcode is no auth. “no CORS”. Phase 3 is both done and listed again as empty. | F-02, F-06, F-07. Phase 3 is partial. Test count is 176. The unset-passcode sentence is accurate and is why F-01 is critical on the funnel recipe. |
| `README.md` lines 250–253 | Two “why no LangGraph” paragraphs. The second says Phase 1 has no tool calls. | Missions and the runner do tool calls. The chat orchestrator does not (`orchestrator.py` lines 1–10). |
| `docs/ARCHITECTURE.md` lines 16, 99, 159–161, 184–185 | Nodes are “Phase 3” future tense in the request path. Risk table lists `android.tap` and `shell.sudo`. Boot returns expired leases to READY and missions resume. | Node code is present. Those tools are not. Resume is not wired. |
| `docs/DECISIONS.md` D-003, D-007, D-019 | No CORS. Depth limit is structural. Telegram is the approval channel. | CORS middleware is on. Depth is stored only. Telegram is a reminder POST. |
| `docs/ROADMAP.md` Phase 3 checkmark | Screenshots, `fs.move`, panels, ansible, automatic reschedule when a node drops. | See “Documented, not implemented”. |
| `docs/SETUP-NODE.md` lines 70–79 | First-word allowlist; empty allowlist only on an untrusted node. | First word is not a shell boundary (F-02). The node does not know `untrusted_ok` (F-11). |
| `app/agent/prompts.py` lines 36–39 | “You do not yet have tools for shell access.” | `operator.yaml` does. |
| `app/main.py` lines 1–9 and 117–120 | API contract frozen for a Phase 2 Ionic app. App version description is “Phase 1”. | Routes have since added missions, memory, nodes, todos. No Ionic app in this repo. |

`docs/DECISIONS.md` D-004 matches the code: shared passcode, `/health` reports `auth_enabled`. D-012 matches `web.search` (`web.py` lines 90–97) and the echo engine. D-014 matches the outbound socket. D-021 matches hybrid search and user delete without approval.

## Recommended fixes

These ids are not defined anywhere in this repository. The split below is the recommended assignment.

### SEC-001 — Auth fails closed, and the passcode stays out of URLs

- Refuse to serve `/api/v1/*` and `/ws/logs` when `JARVIS_PASSCODE` is empty, unless an explicit opt-in is set **and** the process is bound to loopback. Do not treat “funnel + empty passcode” as a supported mode (F-01).
- Replace the WebSocket query passcode with a one-time ticket minted by an authenticated POST, or authenticate `/ws/logs` in the first message the way `/ws/node` does (F-05).
- Add a small failure delay or lockout on bad passcodes (F-12).
- Turn off `/docs`, `/redoc`, and `/openapi.json` unless `JARVIS_DEBUG` is set (F-13).

### SEC-002 — Node execution boundary

- Run allowlisted commands with `shell=False` and an argument vector. Reject metacharacters. Default `--allow` to deny when it is omitted (F-02).
- Run `check_permission` inside `POST /api/v1/nodes/dispatch` and park `DANGEROUS` tools the same way the runner does. The passcode must not be a silent override of invariant 3 (F-03).
- Make `request_id` unguessable and accept a result only from the `node_id` that was dispatched (F-04).
- Cap `shell.exec` timeout on the node and honour the orchestrator timeout by killing the process (F-10).
- Stop treating client-supplied `ram_mb` and capabilities as authority for `EXECUTE` placement, or at least pin capabilities at enrol time (F-04).
- Until a tool sets `untrusted=True`, do not document `untrusted_ok` as an isolation boundary (F-11).

### JAR-004 — Approval and crash recovery actually resume

- After `reconcile_orphaned_tasks`, `scheduler.start` every non-terminal mission (`main.py` lines 88–95, F-06).
- On boot, re-arm `NEEDS_APPROVAL` tasks: keep waiting, or deny them and record that the waiter died. Do not leave a decided approval with no runner (F-06).
- Renew the lease for the whole approval wait, or set the lease from `APPROVAL_TIMEOUT_SECONDS` (F-06).
- Persist each tool call, denial, and approval id. `tool_executions` is already the table (F-16).
- When resume exists, make idempotency apply to the same task’s side effects, or narrow the comment in `models.py` lines 100–102 (F-18).

### JAR-007 — Claims that are ahead of the code

- Delete or enforce `max_depth`. There is no follow-up spawn to refuse (F-15).
- Default `memory.search` to the current mission plus `user`, and require an explicit scope to see other missions (F-09).
- Update `prompts.py` and `is_supported` so chat does not say shell is absent while missions can run `operator` (F-21). Either build or un-check Phase 3 items that are not in the tree: screenshot, `fs.move`, PWA panels, Ansible, the reminder timer, the heartbeat reaper (documentation section above).
- Fix the test-count and “Next: Phase 3” lines in `README.md` and `CLAUDE.md`.

### SYS-004 — Network exposure

- Default `JARVIS_CORS_ORIGINS` to an empty list. Never combine `*` with `allow_credentials` (F-07).
- State in `SETUP-HOST.md` that `0.0.0.0` plus Funnel publishes every route, and that the service must exit if auth is off in that mode (F-01).
- Block `web.fetch` to loopback, link-local, private, and metadata addresses, including across redirects (F-08).
- Run `pytest` with `JARVIS_TEST_DATABASE_URL` pointed at Postgres before the next storage change. This audit did not.

## Assumptions

- The ticket said to create `jarvis/<TICKET-ID>`. The branch is `jarvis/aud-1` (lowercase) so it survives case-insensitive checkouts.
- The ticket also said to change no file except `docs/audit/dhruv-ai-baseline.md`. The sprint handoff that would have been `docs/handoff/AUD-1.md` is the section below, in this file, for that reason.
- Ticket bodies for SEC-001, SEC-002, JAR-004, JAR-007, and SYS-004 are not in the repo. The mapping is a recommendation from the findings, not a reading of those tickets.
- Other named repos were not cloned. Nothing here is a finding about them.
- “Safest” reading of contradictory docs: trust the code, and treat README security claims that are weaker than the code as the bug.

## Handoff

### Summary

Baseline security and completeness audit of dhruv-ai at `8ba215e`. Approval is enforced in the agent runner before tool execution. It is not enforced on `POST /api/v1/nodes/dispatch`. An unset passcode disables every `Guarded` route; that was confirmed, and on the documented Funnel bind it is unauthenticated shell dispatch to any connected node. The node allowlist is bypassed by `shell=True`. Crash recovery rewrites rows and does not resume missions.

### Files changed

- `docs/audit/dhruv-ai-baseline.md` (this file). No other file.

### Commands and results

| Command | Result |
|---|---|
| `pip install -r requirements-dev.txt` | Exit 0. pip warned that `google-genai==0.3.0` is yanked. |
| `python3 -m pytest -q` | **176 passed**, 1 deprecation warning, 7.86s. SQLite. |
| `python3 -m compileall -q app nodes mcp_server.py` | Exit 0. |
| `ruff` / `mypy` | Not configured and not installed. Not run. |
| Postgres pytest | Not run. No server. |
| Auth-off `TestClient` probe | `/health` `auth_enabled=false`. Greeting without a header: 200. `shell.exec` dispatch: 503 no node, not 401. `/openapi.json`: 200, 32 paths. |
| Allowlist probe in a temp directory | `echo` allowlist executed `echo hi; echo injected-marker`. Bare `rm -rf /` raised `ToolError`. Empty allowlist executed `echo`. |

### Known gaps

Postgres not re-run. No live node, Funnel, or provider key. Path-jail TOCTOU not tested. Yanked-dependency CVE not verified. SEC/JAR/SYS ticket text not available, so the mapping may need a reshuffle when those tickets are opened.

### Security impact of this change

None at runtime. Documentation only. It records that the current tree will run node shells without approval when the passcode is unset, and that a non-empty allowlist is not a shell boundary.

### Rollback

Delete `docs/audit/dhruv-ai-baseline.md` and remove the commit from the branch. No schema, config, or dependency change to undo.
