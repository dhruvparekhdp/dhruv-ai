# Decision log

Why this system is shaped the way it is. Read before changing architecture — several entries exist
because the obvious choice was wrong for a non-obvious reason.

---

## D-001 · Groq model is `openai/gpt-oss-120b`

`llama-3.3-70b-versatile` is retired on **2026-08-16**. `openai/gpt-oss-120b` is Groq's own
recommended replacement. Overridable via `GROQ_MODEL`; do not set it back.

---

## D-002 · Storage is dual-dialect (SQLite + Postgres)

Postgres in production, SQLite for local development and tests — so tests need no database server and
run offline. Every query goes through `app/core/db.py`, where `q()` rewrites `?` → `%s` and
`ddl_types()` swaps the divergent type names.

**Cost:** all storage changes must be tested on both. Four dialect differences have already caused
real bugs — placeholders, JSON columns returning `str` vs `dict`, multi-statement DDL (psycopg
rejects it in one `execute`), and timestamp types (D-005).

---

## D-003 · Single-origin PWA served by FastAPI

One service, one certificate, no CORS. The API contract is frozen so the PWA in `app/static/` can be
replaced by a framework later without touching the backend.

**Cost:** no framework ecosystem. Accepted; the constraint keeps the UI simple.

---

## D-004 · Shared passcode now, JWT + passkeys later

A single constant-time-compared `JARVIS_PASSCODE` on every `/api/v1/*` route and the WebSocket
handshake. Small, and enough to keep a publicly reachable URL from being open to anyone who finds it.

`/health` reports `auth_enabled`, so an unprotected instance is visible rather than silent.

---

## D-005 · Timestamps are application-generated with microsecond precision

`CURRENT_TIMESTAMP` has only second granularity. Check-ins made in the same second tied, and
`ORDER BY` fell through to a random UUID — **silently reversing the mood trend chart**.

Two rules follow:

- Write timestamps from Python with `isoformat(timespec="microseconds")` — fixed width, so SQLite's
  lexicographic sort stays chronological.
- **Never compare timestamps as strings across backends.** SQLite returns the ISO string written;
  Postgres returns a `datetime` whose `str()` uses a space instead of `T`. `_parse_ts()` in
  `missions/store.py` normalises both. This one broke only on Postgres — it would have passed every
  local test and failed crash recovery in production.

---

## D-006 · Agents are declarative records, not classes

The roadmap needs ~20 specialists. Twenty classes would each re-implement the same tool-calling loop
slightly differently, and the permission model would drift across them.

An agent is a YAML file — identity, prompt, tools, capabilities, model preference, budget — executed
by one generic `AgentRunner`. Definitions are validated at boot, so a typo fails loudly at startup
rather than mid-mission.

**Adding an agent is adding a file.** If you are writing an agent class, something has gone wrong.

---

## D-007 · Agents cannot call agents · **invariant**

Enforced structurally, not by convention:

- the runner exposes tools, never other agents;
- an agent returns a result or *requests* follow-up work the scheduler may refuse;
- `MissionScheduler` solely owns the task graph, and every dispatch is persisted to `agent_messages`;
- every task carries a `depth`; spawning past `max_depth` is refused, not queued.

Centralising dispatch in one audited place is the whole mechanism. Do not add a shortcut.

---

## D-008 · Permissions live at the tool boundary, not the agent · **invariant**

Danger lives in what gets done, not in who asked. Tools declare a `Capability` and a `Risk`; agents
hold grants. Both gates must pass — the agent's tool allowlist *and* its capability grants — so a
typo in a YAML file cannot hand an agent the ability to spend money.

Agents are never shown a tool they cannot call, which prevents most violations before they happen.
`DANGEROUS` tools always park for approval regardless of grants.

---

## D-009 · Memory is a tool, not shared state · **invariant**

If agents shared a memory object, context would leak between missions by default and the permission
model would be unenforceable. Memory is scoped (`user` / `project` / `mission`) and reached only
through audited, permissioned tool calls.

Context for a task is assembled from its explicit dependency results — never inherited from a growing
global buffer. That is the mechanism behind "no context leak".

---

## D-010 · Leases, not assignments; reconcile at boot

Running 24/7 means surviving crashes and restarts without human intervention.

- A task is **leased** to a runner for N seconds. If the runner dies the lease lapses and boot
  reconciliation returns the task to `READY`. An assignment would hang forever.
- **Boot reconciliation** runs before serving traffic: tasks stuck in `RUNNING` with a lapsed or
  missing lease are requeued; mid-flight missions resume from their task graph.
- **Idempotency keys** stop a retry from re-running work that already succeeded — essential once a
  task has side effects (a message sent, a tap on a real phone).

---

## D-011 · Budgets are checked before every dispatch · **invariant**

Free API quotas are small (Groq: 14,400 requests/day). One planning loop could burn a day's quota in
minutes. Per-mission caps on tasks, depth, tokens, wall-clock and tool calls are checked *before*
dispatch, so exhaustion fails the mission with a stated reason instead of hanging or overspending.

`MAX_ITERATIONS` in the runner is a second, independent ceiling on model round-trips.

---

## D-012 · Honest degradation over simulated success · **invariant**

- No API key → a local `echo` engine that says it is the fallback.
- No search key → `web.search` reports itself unconfigured and suggests `web.fetch`. It does **not**
  invent plausible results.
- Unsupported intent → the agent says the subsystem is not wired up rather than claiming to have run
  something.
- The echo engine emits **no tool calls**, because it cannot reason about when a tool is warranted and
  inventing them would produce confidently wrong actions.

A fabricated answer poisons the reply *and* the training data collected from it.

---

## D-013 · An iPhone can report but never execute · **hard platform limit**

iOS sandboxing forbids inter-app automation. No sideload or entitlement changes this. The owner's
primary phone is a dashboard and approval gate, permanently.

Telemetry still works via an **iOS Shortcuts** automation ("when I connect to <Wi-Fi>" → battery,
device, network → POST to the API) — no jailbreak, no Mac.

The device registry marks this explicitly: `capabilities: [REPORT]` vs `[REPORT, EXECUTE]`, so the
limit reads as a designed boundary rather than a bug.

---

## D-014 · Devices dial out; nothing dials in · **hard constraint**

Phones and laptops sit behind NAT. There is no "Jarvis connects to phone." Every node opens an
outbound WebSocket and holds it; commands travel down the established socket, results back up.

This also makes enrolment clean (token → node session), and makes the host laptop simply the largest
node rather than a required hub.

---

## D-015 · Android control uses the companion app, not ADB

| | AccessibilityService app | ADB over Wi-Fi |
|---|---|---|
| Tap latency | ~2 ms (in-process `dispatchGesture`) | 300–500 ms |
| Android 14+ | works | **port randomised after sleep — breaks automation** |
| Setup | install + one toggle | re-pair after every reboot |

**Cost:** Google Play rejects this class of app. Personal sideload only — acceptable for a
single-user system.

---

## D-016 · The host is the laptop

Jarvis runs on a dedicated machine (i5-1235U, 16 GB, Debian 13) and is reached from anywhere through
a **Tailscale Funnel** URL — a stable public HTTPS hostname with a valid certificate, no domain
purchase required.

16 GB and 10 cores also make a local Ollama fallback engine viable, which no small rented instance
would.

**Cost accepted:** if the laptop is off, Jarvis is down. Reasonable for a single-user assistant.

Cloud *inference* (Groq, Gemini) is unaffected; only hosting is local.

---

## D-017 · Blobs go to object storage, never the database

Vision-agent screenshots run ~300 KB each and would bloat the database within days of device
automation. Screenshots go to S3-compatible object storage with only a URL and hash in Postgres.

**What controls cost is retention, not capacity** — prune screenshots after N days and compact agent
messages once a mission completes.

---

## D-018 · Debian 13 with XFCE, not headless

Headless would save ~1.5 GB of RAM on a 16 GB machine — the wrong thing to optimise. The owner has
not operated a Linux box without a GUI and would be unable to do basic file management.

XFCE (~600 MB idle) leaves ~15 GB for Jarvis and a local model, and provides a file manager, editor
and terminal. **Cockpit** adds point-and-click admin from another machine's browser.

Jarvis will grow its own Files/System/Terminal panels (Phase 3), but **it can never be the only way
in** — you cannot use Jarvis to fix Jarvis, and a 2 a.m. crash needs a path that does not depend on
the thing that crashed. XFCE + Cockpit is the floor; Jarvis is the ceiling.

---

## D-019 · Telegram is the gateway and the approval channel

Free, works from anywhere, and — the deciding factor — **inline keyboard buttons are the HITL
approval gate**, pushed to any phone with no web-push setup. That solves approvals on the iPhone,
otherwise the weakest surface in the system.

Voice arrives as voice message → Whisper → mission → TTS reply. Not a live call; Discord is the
upgrade path if full-duplex voice is ever wanted.

---

## D-020 · No LangGraph yet

The scheduler is a dependency-driven loop over a persisted task graph; a graph library would wrap
that rather than simplify it, and persistence, leases and budgets are the parts that actually matter.

The nodes are already separated (`router` → `runner` → `scheduler`), so adopting it when the coding
and device subsystems introduce genuine cycles stays a contained change.


---

## D-021 · Memory search is hybrid, local, and brute-force

**Retrieval is hybrid.** Vector search misses exact terms; keyword search misses paraphrase.
Both rankings are fused with Reciprocal Rank Fusion, which combines them without needing their
scores to be on comparable scales -- they are not.

**Embeddings run locally** (FastEmbed, ONNX, no PyTorch). Embedding happens on every write and
every search, which would exhaust a daily API quota quickly, and keeping it local means memory
search still works offline. If the model cannot be fetched, `available()` returns False and search
falls back to keyword-only while *saying so* -- a thin result set must not be mistaken for an
empty memory.

**No vector database, and no pgvector.** 10,000 memories at 384 dimensions is ~15 MB, and scoring
that in Python takes milliseconds. Brute force is fine to roughly 100k entries -- years of personal
use. A Postgres extension would break SQLite parity (D-002) for no measurable gain at this scale.
`store.candidates()` is the seam to change when the numbers eventually demand it.

**Embeddings are stored as base64 float32 in a TEXT column**: ~2 KB per entry (vs ~8 KB as JSON)
and byte-identical on both backends, so no new dialect handling.

**Every entry carries provenance** -- when it was learned and which agent or mission wrote it -- and
the Memory panel makes all of it browsable, editable and deletable. A confidently recalled wrong
fact is worse than no memory, and the only defence is being able to see and correct what the system
believes.

**User deletion needs no approval gate.** That gate exists to hold *agents* accountable to the
human; the human deleting their own memory is the accountability, not a thing to be checked.


---

## D-022 · Dependency policy: no third-party *services*, libraries are fine

The requirement was "99% from scratch, avoid third-party library". Taken literally that means
hand-writing an HTTP server, WebSocket framing, TLS and the Postgres wire protocol — months of work
for a less secure result, and Jarvis would never ship.

The line that actually serves the goal is **logic vs plumbing**:

- **Write the logic.** Orchestration, scheduling, permissions, retrieval, risk gates. Already true:
  the BM25 ranking, rank fusion, cosine similarity, mission scheduler, lease/reconciliation and
  permission model are all hand-written, not imported.
- **Import the plumbing.** FastAPI, uvicorn, psycopg. Boring, replaceable, and not where the value is.
- **No third-party services.** No SaaS, no hosted memory, no vendor lock-in, nothing phoning home.
  This is the part that matters for sovereignty, and it is absolute.

Rule of thumb: no new library for anything writable in ~200 lines. New dependencies get a line here
explaining why.

Worth noting the source plan's own "from scratch" KVM imports `pynput`, `pyperclip` and `pyautogui` —
three third-party libraries in the snippet meant to demonstrate avoiding them.

---

## D-023 · Nodes advertise capabilities; roles are not hardcoded

The source plan assigns fixed roles to fixed IPs — *Node 3 = sandbox at 192.168.1.103*. That breaks
the moment a laptop is added, removed, repurposed or simply off.

Instead a node registers what it **can do**, and the scheduler places work against those
capabilities:

```yaml
node_id: bench-01
ram_mb: 16384
cores: 10
capabilities: [EXECUTE, INFERENCE, STORAGE]
untrusted_ok: false
```

Consequences:

- the fleet works with one node or five, with no config change anywhere;
- a node going offline reschedules its work automatically (the lease machinery in D-010 already
  does this);
- hardware can be repurposed by editing one line on that node;
- the count genuinely stops mattering, which is what "make it dynamic" requires.

Current hardware settles naturally into: i5/16 GB → master (orchestrator, Postgres, memory);
i3/12 GB → inference (a 7B model fits in 12 GB); i5/8 GB → sandbox (isolation matters more than
speed). None of that is written into code.

---

## D-024 · Worker nodes are headless; the KVM is deferred

The main node keeps XFCE (D-018 — the owner has to be able to operate it). Spare laptops run
**headless**, lid closed, reached over SSH and Cockpit.

That decision removes the KVM requirement rather than solving it: a shared keyboard/mouse exists to
drive several *monitors*, and headless nodes have none. The source plan asks for both headless
workers and a KVM across them, which is self-contradictory.

Two further reasons the scratch-built KVM was declined:

- the supplied code uses `pynput`/`pyautogui`, which are **X11-only**. Debian 13 defaults to
  **Wayland**, where global input capture is blocked by design — so it would not run on the very OS
  the same document recommends;
- the source plan rates it ~40% rework, mostly clipboard feedback loops and display-server quirks.

Revisit only if monitors are attached to the spare machines. Deskflow solves it for free in the
meantime.

---

## D-025 · Clocks must be synchronised across nodes

Ordering in this system depends on microsecond timestamps (D-005), and a mood chart has already
rendered backwards once from a timing bug. Across several machines with drifting clocks, task
ordering and lease expiry break in ways that are extremely hard to debug — a node could expire a
lease that has not actually lapsed, and duplicate work.

`chrony` on every node, installed by the provisioning playbook. Cheap insurance against a whole
class of distributed-systems bug.
