# Jarvis — project context

Personal AI assistant built as a **multi-agent orchestrator with a device fleet**. A planner
decomposes a goal into tasks, specialist agents execute them under permission and budget limits,
and (from Phase 3) those tasks run on real machines.

Owner: Dhruv. Single-user system — there is no multi-tenancy anywhere, by design.

**Read `docs/DECISIONS.md` before changing architecture.** Most of the surprising choices in this
codebase are deliberate and have a recorded reason.

---

## The four layers

Keep these separate. Collapsing them is how this becomes unmaintainable.

| Layer | Question it answers | Lives in |
|---|---|---|
| **Orchestration** | what happens, in what order | `app/missions/` |
| **Agents** | who decides | `app/agents/` |
| **Tools** | what can be done | `app/tools/` |
| **Nodes** | where it runs | `nodes/` (Phase 3) |

**Agents decide, tools execute, nodes are where tools run.** An agent with no granted capabilities
is harmless regardless of what the model produces.

---

## Non-negotiable invariants

Breaking any of these is a bug, not a refactor:

1. **Agents never call agents.** They return a result, or *request* follow-up work the scheduler may
   refuse. All dispatch goes through `MissionScheduler`. This is what makes runaway recursion
   structurally impossible rather than merely discouraged.
2. **Permissions are enforced at the tool boundary**, never the agent. Danger lives in what gets
   done, not who asked.
3. **`DANGEROUS` tools always park for human approval.** No exceptions, no agent-level override.
4. **Nothing important lives only in RAM.** Every mission/task transition is written immediately, so
   a crash costs in-flight latency and nothing else.
5. **Context is assembled per task** from its explicit dependencies. There is no growing global
   buffer — that is what keeps one mission's context out of another's.
6. **Memory is a tool with permissions**, not shared state. Every entry records where it came
   from, and the user can inspect, correct and delete all of it.
7. **Budgets are checked before every dispatch.** Exhaustion is a reported outcome, never a hang.
8. **Nodes dial out; nothing dials in.** Placement is by capability, never hostname. A tool needing
   a node that is not connected is *refused* — never silently run locally.
9. **Never fabricate.** A tool that cannot do its job says so (see `web.search` with no key). A
   fabricated answer poisons both the reply and the training data collected from it.

---

## Commands

```bash
pip install -r requirements-dev.txt
JARVIS_PASSCODE=dev uvicorn app.main:app --reload      # http://127.0.0.1:8000, passcode "dev"

pytest -q                                              # SQLite (default)
JARVIS_TEST_DATABASE_URL=postgresql://... pytest -q     # Postgres — run before touching storage
```

Works with **no API key**: the engine router falls back to a local echo engine and says so. Tests
never touch the network or spend a token.

---

## Layout

```
app/
  main.py                  FastAPI: routes, PWA mounting, lifespan (init + boot reconciliation)
  core/
    config.py              env-driven settings, single source of truth
    db.py                  dual-dialect SQLite/Postgres; q() rewrites ? -> %s
    telemetry.py           the fine-tuning flywheel (what was said, how good it was)
    security.py            passcode gate (Phase 1); JWT + passkeys later
  agent/                   the CHAT fast path — "hi" must not spin up a mission
    router.py              intent classification, word-boundary matched
    engines.py             Groq | Gemini | echo, with tool-calling and fallback chain
    orchestrator.py        single-turn pipeline (chat + mood)
    prompts.py
  agents/                  the SPECIALISTS
    definitions/*.yaml     an agent is a file, not a class
    registry.py            load + validate at boot; bad definition fails loudly
    runner.py              ONE generic tool-calling loop for every agent
  missions/                ORCHESTRATION
    models.py              Mission/Task/Budget + statuses
    store.py               durable state (separate from telemetry — operational, not training)
    planner.py             goal -> task graph, parsed defensively
    scheduler.py           the only thing that dispatches work
  memory/                  the brain
    store.py               scoped entries with provenance
    embeddings.py          local ONNX embeddings, optional at runtime
    search.py              hybrid retrieval: vector + BM25, fused
  nodes/                   the fleet
    models.py              Node, capabilities, statuses
    store.py               durable registry; secrets stored hashed
    gateway.py             live sockets, capability placement, dispatch
  tools/
    registry.py            capability + risk + JSON schema; permission check lives here
    cloud/                 memory, web (run in-process)
    node/                  fs, shell, system (declared here, run on a node)
nodes/pc/agent.py          the agent that runs on each machine
  services/websocket_manager.py
  static/                  vanilla PWA, no build step
docs/                      ARCHITECTURE, DECISIONS, ROADMAP, SETUP-HOST, SETUP-NODE
drills/                    debugging drills: planted bugs + a runner (see drills/README.md)
```

---

## Conventions

- **Comments explain *why*.** The what is readable from the code. Where a choice looks odd, the
  comment says what breaks otherwise.
- **Degrade honestly.** No key, no node, no search API → say so plainly. Never simulate success.
- **Both backends.** Any storage change must pass the suite on SQLite *and* Postgres. They differ in
  placeholders, JSON columns, multi-statement DDL, and timestamp types — all four have already
  caused real bugs.
- **Adding an agent is a YAML file.** If you find yourself writing an agent class, stop.
- **Adding a tool is a `@tool` decorator** with an honest capability and risk level.

---

## Current state

Phases 1 and 2 are done: chat, mood check-ins, telemetry flywheel, PWA, and the full orchestration
substrate (missions, agents, tools, permissions, approvals, budgets, crash recovery).
**150 tests, passing on SQLite and Postgres 16.**

**Next: Phase 3** — node protocol and the PC node agent, running on the host laptop.

### The fleet (decided)

**One master, N workers. The count is dynamic — never hardcode it.**

| Machine | Role today | Desktop |
|---|---|---|
| i5 / 16 GB | master: orchestrator, Postgres, memory, PWA | XFCE (D-018) |
| i3 / 12 GB | inference: Ollama 7B fallback, Whisper, TTS | headless |
| i5 / 8 GB | sandbox: untrusted code, browser automation | headless |

Roles are **not** in code. Each node advertises `capabilities` and the scheduler places work against
them (D-023), so adding, removing or repurposing a machine is a one-line change on that node.

**The host is primary. There is no cloud VM.**

```
iPhone / any browser
   │  HTTPS via Tailscale Funnel (free, stable *.ts.net, no domain, no card)
   ▼
Host laptop — Debian 13 + XFCE, i5-1235U, 16 GB
   Jarvis orchestrator + PC node + (later) Ollama local fallback
```

Cloud inference (Groq, Gemini) still happens over the internet; only *hosting* is local.

---

## Hard platform limits — do not design around these, they do not move

- **An iPhone can report but never execute.** iOS sandboxing forbids inter-app automation. iPhone
  telemetry arrives via a Shortcuts automation POSTing to the API. Execution is Android-only.
- **The cloud cannot reach devices behind NAT.** Every node dials *out* and holds the socket.
- **Android control needs the companion app, not ADB.** Android 14+ randomises the ADB port after
  sleep, which breaks automation outright.
