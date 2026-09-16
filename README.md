# Jarvis

A personal AI assistant built as a **multi-agent orchestrator**: a planner decomposes your goal into
tasks, specialist agents execute them under permission and budget limits, and every step is recorded
as training data.

Runs on your own hardware, and works before you have any API key at all.

| Document | What's in it |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Project context and the invariants that must not be broken |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pieces fit and why the seams fall where they do |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why each choice was made — **read before changing architecture** |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | What's built, what's next |
| [`docs/SETUP-HOST.md`](docs/SETUP-HOST.md) | Setting up the host laptop, step by step |
| [`docs/SETUP-NODE.md`](docs/SETUP-NODE.md) | Joining another machine to the fleet |
| [`docs/TODOS.md`](docs/TODOS.md) | The todo module, and connecting it to Claude over MCP |
| [`drills/README.md`](drills/README.md) | **Debugging drills** — learn this codebase by fixing real bugs in it |

---

## What works right now

**Orchestration**
- **Missions** — give Jarvis a goal; the planner decomposes it into a task graph and runs the
  independent branches concurrently.
- **Agents as data** — an agent is a YAML file (prompt, tools, capabilities, budget), executed by one
  generic runner. Adding an agent is adding a file, not writing code.
- **Permissions at the tool boundary** — every tool declares a capability and a risk level. An agent
  is never even shown a tool it cannot call.
- **Human approval** — `DANGEROUS` tools park and wait for a decision in the PWA. Nothing executes
  meanwhile.
- **Budgets and loop guards** — per-mission caps on tasks, depth, tokens, time and tool calls.
  Exhaustion fails the mission with a reason instead of burning your API quota.
- **Crash recovery** — all state is written to the database at every transition. On boot, tasks
  orphaned by a crash are requeued via expired leases and missions resume from their task graph.
- **Audited message bus** — every dispatch and result is persisted; agents never call each other.

**Fleet**
- **Nodes dial out** — any machine (Linux, Windows, macOS) joins with a one-time token and holds one
  outbound connection. No open ports, no port forwarding.
- **Capability-based placement** — a node advertises what it can do; the scheduler places work
  against that, never against a hostname. Works with one machine or five.
- **Enforced on the node** — a path jail that resolves `../` before checking, and a shell allowlist.
  Both hold even if the orchestrator is compromised.
- **Untrusted code is opt-in** — model-generated code runs only on a node you explicitly clear. With
  none cleared, the work is refused rather than run somewhere trusted.

**Memory**
- **Hybrid search** — semantic (local embeddings) fused with keyword ranking, so it matches both
  paraphrase and exact terms. Falls back to keyword-only and says so if the model is unavailable.
- **Provenance on everything** — when each fact was learned and which agent wrote it.
- **Memory panel** — browse, search, correct and delete everything Jarvis believes about you.

**Todos** — see [`docs/TODOS.md`](docs/TODOS.md)
- **Tracks with a WIP limit** — at most two active at once, enforced in the store. Activating a
  third is refused, and the refusal names what is already active.
- **`next` returns one item**, never a list, and prefers work already started over starting
  something new. Finishing beats starting.
- **MCP server** — Claude gets `todo_next`, `track_summary`, add, list, update and track control.
- **Telegram reminders**, swept on a timer; unconfigured means nothing is sent *and* nothing is
  marked as reminded.

**Assistant**
- **Chat** and **mood check-ins** with a 14-day trend strip.
- **Telemetry flywheel** — each turn recorded with its system prompt, provider, model, tokens and
  latency; thumbs up/down becomes a reward signal.
- **Dataset export** — ShareGPT JSONL, ready for Unsloth / Axolotl / LLaMA-Factory.
- **Live WebSocket stream** and an **installable PWA**.

### Agents and tools shipped

| Agent | Tools | Capabilities |
|---|---|---|
| `planner` | — | — (decomposes goals) |
| `research` | `web.fetch`, `web.search`, `memory.search`, `memory.read` | READ |
| `memory` | `memory.search`, `memory.read`, `memory.write`, `memory.forget` | READ, WRITE |
| `assistant` | `memory.search`, `memory.read` | READ |
| `operator` | `fs.*`, `shell.exec`, `system.stats`, `memory.search` | READ, WRITE, EXECUTE |

| Tool | Capability | Risk |
|---|---|---|
| `web.fetch`, `web.search`, `memory.search`, `memory.read` | READ | SAFE |
| `memory.write` | WRITE | SENSITIVE |
| `fs.list`, `fs.read`, `system.stats` | READ | SAFE (on a node) |
| `fs.write` | WRITE | SENSITIVE (on a node) |
| `memory.forget`, `fs.delete`, `shell.exec` | WRITE / EXECUTE | **DANGEROUS** — always needs approval |

Not built yet: the Android companion, vision-verified automation, the coding agent, trading, and voice. Agents say so
plainly rather than pretending to have run something.

---

## Quick start (local)

```bash
pip install -r requirements-dev.txt
JARVIS_PASSCODE=dev uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and enter `dev`.

No API key needed — Jarvis falls back to a local engine and tells you it's doing so.

```bash
pytest -q          # 150 tests, no network, no tokens spent
```

The same suite runs against either backend. The host runs Postgres while local development runs on
SQLite, so both are tested:

```bash
pytest -q                                              # SQLite (default)
JARVIS_TEST_DATABASE_URL=postgresql://... pytest -q    # Postgres
```

---

## Deployment

Jarvis runs on a dedicated host laptop, reached from anywhere through a Tailscale Funnel URL — a
stable public HTTPS hostname with a valid certificate, no domain purchase.

```
iPhone / any browser
   │  HTTPS · https://<machine>.<tailnet>.ts.net
   ▼
Host laptop — Debian 13 + XFCE · i5-1235U · 16 GB
   Jarvis orchestrator + PC node + local Postgres + (later) Ollama
        │
        └── cloud inference: Groq (fast) · Gemini (deep)
```

**Full walkthrough: [`docs/SETUP-HOST.md`](docs/SETUP-HOST.md).**

API keys — both optional, and Jarvis runs without either:
- Groq — <https://console.groq.com> → `GROQ_API_KEY`
- Google AI Studio — <https://aistudio.google.com> → `GEMINI_API_KEY`

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_PASSCODE` | _(none)_ | Required on all API calls. Unset = **no auth at all**. |
| `GROQ_API_KEY` | _(none)_ | Fast engine. |
| `GEMINI_API_KEY` | _(none)_ | Deep-reasoning engine. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | See the model note below. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `DATABASE_URL` | _(none)_ | Unset → SQLite. Postgres URL → durable storage. |
| `JARVIS_DB_PATH` | `jarvis_system.db` | SQLite file location. |
| `JARVIS_DEBUG` | `false` | Verbose logging. |

> **Model note.** The original design document specified `llama-3.3-70b-versatile` as the primary
> Groq engine. Groq is shutting that model down on **2026-08-16**, so the default here is
> `openai/gpt-oss-120b` — Groq's own recommended replacement. Don't set it back.

---

## API

Every `/api/v1/*` route requires the `X-Jarvis-Passcode` header. The WebSocket takes it as a query
parameter, because browsers can't set headers on a WS handshake.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, engine + storage state. **Unauthenticated** (for uptime probes). |
| `GET` | `/api/v1/greeting` | Time-aware greeting and the mood scale. |
| `POST` | `/api/v1/session/start` | Mint a session id. |
| `GET` | `/api/v1/session/{id}` | Cheap existence check. |
| `POST` | `/api/v1/command` | Main turn. |
| `POST` | `/api/v1/mood` | Log a check-in and reply to it. |
| `GET` | `/api/v1/mood/history` | Recent check-ins. |
| `POST` | `/api/v1/feedback` | Attach a ±1 reward to a turn. |
| `GET` | `/api/v1/telemetry/stats` | Counters. |
| `GET` | `/api/v1/dataset/export` | ShareGPT JSONL download. |
| `POST` | `/api/v1/missions` | Create and start a mission. |
| `GET` | `/api/v1/missions` | Recent missions. |
| `GET` | `/api/v1/missions/{id}` | Mission with its task graph and progress. |
| `GET` | `/api/v1/missions/{id}/messages` | Audited message bus for one mission. |
| `POST` | `/api/v1/missions/{id}/cancel` | Cancel a running mission. |
| `GET` | `/api/v1/agents` | Loaded agent definitions. |
| `GET` | `/api/v1/tools` | Tools with capability and risk. |
| `GET` | `/api/v1/nodes` | The fleet: every node, its capabilities and health. |
| `POST` | `/api/v1/nodes/enrol` | Mint a one-time join token. |
| `POST` | `/api/v1/nodes/dispatch` | Run a node tool directly. |
| `POST` | `/api/v1/nodes/{id}/trust` | Clear a node for untrusted code. |
| `DELETE` | `/api/v1/nodes/{id}` | Revoke a node. |
| `WS` | `/ws/node` | Node connection endpoint. |
| `GET` | `/api/v1/approvals` | Actions parked awaiting a decision. |
| `POST` | `/api/v1/approvals/{id}` | Approve or deny. |
| `GET` | `/api/v1/memory` | Browse stored facts. |
| `GET` | `/api/v1/memory/search` | Hybrid search. |
| `POST` | `/api/v1/memory` | Store a fact. |
| `PUT` | `/api/v1/memory/{id}` | Correct a fact. |
| `DELETE` | `/api/v1/memory/{id}` | Delete a fact. |
| `WS` | `/ws/logs` | Live telemetry stream. |

This contract is frozen — the Phase 2 dashboard is built against it, so the vanilla PWA in
`app/static/` can be replaced without touching the backend.

---

## Architecture

```
iPhone / browser  ──HTTPS──►  FastAPI (single origin: API + PWA, no CORS)
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              intent router  engine router  telemetry
              4 categories   Groq│Gemini    SQLite│Postgres
                             └─ echo fallback      │
                                                   ▼
                                        ShareGPT JSONL export
```

```
app/
  main.py                       routes, PWA mounting, lifespan
  core/        config · db · telemetry · security
  agent/       router · engines · orchestrator · prompts   (chat fast path)
  agents/      registry · runner · definitions/*.yaml      (the specialists)
  memory/      store · embeddings · search                 (the brain)
  nodes/       models · store · gateway                    (the fleet)
nodes/pc/      the node agent that runs on each machine
  missions/    models · store · planner · scheduler        (orchestration)
  tools/       registry · cloud/{memory,web}               (capability + risk)
  services/    websocket_manager
  static/      the PWA (vanilla — no build step)
tests/         150 tests, all offline
```

**Why one runner instead of a class per agent:** the roadmap calls for ~20 specialists. Twenty
classes would each re-implement the same tool-calling loop slightly differently. A YAML definition
plus one runner keeps that growth free.

**Why agents cannot call agents:** an agent returns a result or *requests* follow-up work that the
scheduler may refuse. Centralising dispatch is what makes runaway recursion structurally impossible
rather than merely discouraged.

**Why no LangGraph yet:** the nodes are already separated (`router` → `runner` → `scheduler`), so
introducing it when the coding and device subsystems add real cycles stays a contained change.

**Why one service instead of separate frontend and backend hosts:** a single origin means one
deploy, one certificate, no CORS, and no second signup — which is what makes this deployable in
half an hour.

**Why no LangGraph yet:** Phase 1 has no branching, no tool calls, and no loops. A graph library
would be ceremony around a straight line. The nodes are already separated (`router.py`,
`engines.py`, `orchestrator.py`), so introducing it when the coding and trading subsystems add real
cycles is a contained change.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | FastAPI core, telemetry flywheel, mood check-in, PWA | **done** |
| 2 | Missions, agents, tools, permissions, approvals, durability | **done** |
| 2.6 | Memory search: hybrid retrieval, provenance, Memory panel | **done** |
| 3 | Node protocol, capability registry, PC node agent | **done** |
| 2.5 | Device registry, iPhone telemetry via Shortcuts, Telegram gateway | next |
| 3 | Node protocol + PC node (shell, files, screenshots) | |
| 4 | Android companion app (AccessibilityService) | |
| 5 | Vision-verified device automation | |
| 6 | Voice, more agents, fine-tuning pipeline | |

**Devices dial out, never in.** A cloud host cannot reach a phone behind NAT, so every node opens an
outbound WebSocket and holds it. That also makes the laptop just the largest node rather than a
required hub — if it is off, Jarvis still answers.

**An iPhone can report but never execute.** iOS sandboxing forbids inter-app automation. iPhone
telemetry arrives via a Shortcuts automation POSTing to `/api/v1/devices/checkin`; execution is
Android-only.

---

## Security notes

- The passcode is a single shared secret — enough to keep a reachable URL from being wide open, not
  a substitute for real auth. JWT + WebAuthn passkeys come later.
- Secrets are read from the environment and handed only to the engine client that needs them.
  Nothing secret is committed; `.env` is gitignored.
- Gemini's free tier may use prompts to improve Google's products. Fine for mood check-ins; worth
  remembering before piping private code or financial positions through it.
- Jarvis is explicitly instructed not to present as a therapist, and to say when a real person
  would help more.
