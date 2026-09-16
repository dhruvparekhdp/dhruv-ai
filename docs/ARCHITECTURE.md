# Architecture

How Jarvis is put together, and why the seams fall where they do.

For the reasoning behind individual choices see [`DECISIONS.md`](DECISIONS.md); for what is built
and what is next see [`ROADMAP.md`](ROADMAP.md).

---

## The separation that everything else follows

```
Orchestration   what happens, in what order      app/missions/
Agents          who decides                      app/agents/
Tools           what can be done                 app/tools/
Nodes           where it runs                    nodes/          (Phase 3)
```

**Agents decide. Tools execute. Nodes are where tools run.**

The fourth layer is the one most easily collapsed and the most costly to lose. Without it every
agent re-implements shell access, and permissions become unenforceable because there is no single
place to check them.

---

## System shape

```
                    USER  (PWA · Telegram · voice)
                              │
                  ┌───────────▼────────────┐
                  │  JARVIS ORCHESTRATOR   │  owns the task graph.
                  │  planner · scheduler   │  nothing else dispatches work.
                  │  budgets · approvals   │
                  └───────────┬────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
       AGENT REGISTRY    MESSAGE BUS       MEMORY
       YAML definitions  persisted,        user · project · mission
       validated at boot audited           (a tool, not shared state)
              │
              ▼
       ┌─────────────┐
       │ AgentRunner │  ONE generic loop runs every agent
       └──────┬──────┘
              │   agents request tools; never other agents
              ▼
       ┌─────────────┐
       │TOOL REGISTRY│  capability + risk + JSON schema
       └──────┬──────┘
              │
   ┌──────────┼──────────────┐
   ▼          ▼              ▼
CLOUD      NODE           APPROVAL GATE
TOOLS      TOOLS          DANGEROUS tools park here
memory     shell, files,  until a human decides
web        screenshot,
           android.tap
              │
              ▼
      ┌───────────────┐
      │ NODE GATEWAY  │  nodes dial OUT and hold the socket
      └───────┬───────┘
              │
   ┌──────────┼──────────┬──────────┐
   ▼          ▼          ▼          ▼
Host laptop  Android#1  Android#2  iPhone
(PC node)    (execute)  (execute)  (report only)
```

---

## Request paths

There are two, and keeping them separate matters.

**Fast path — chat and mood.** `"hi"` must not spin up a mission.

```
POST /api/v1/command
  → agent/router.classify_intent()      word-boundary matched, no LLM call
  → agent/orchestrator.run_turn()        single turn
  → agent/engines.EngineRouter           Groq | Gemini | echo
  → telemetry.log_trajectory()           the flywheel
```

**Mission path — anything that needs decomposition.**

```
POST /api/v1/missions
  → scheduler.create_mission()           persisted immediately
  → planner.plan_mission()               goal -> task graph (parsed defensively)
  → scheduler._execute_graph()           loop: find ready tasks, run concurrently, record
      → runner.run()                     per task, bounded tool-calling loop
          → permission check             capability + allowlist
          → approval gate                if DANGEROUS
          → tool handler                 cloud now, node from Phase 3
  → mission COMPLETED / FAILED           summary persisted
```

---

## Storage split

Two stores, one database, different purposes. Do not merge them.

| | `core/telemetry.py` | `missions/store.py` |
|---|---|---|
| Owns | what the model said, how good it was | what is running, what happens next |
| Tables | `sessions`, `agent_trajectories`, `tool_executions`, `mood_checkins` | `missions`, `tasks`, `agent_messages`, `approvals` |
| Consumer | fine-tuning dataset export | the scheduler |
| Lifetime | keep forever (it is the training set) | prunable once missions complete |

A third store, `memory/store.py`, holds what Jarvis *knows* -- scoped facts with provenance and
embeddings, searched by `memory/search.py`.

All three go through `core/db.py`, which handles the SQLite/Postgres dialect gap.

### Memory retrieval

```
memory.search("how do I sleep")
   ├── vector   cosine over local ONNX embeddings   (matches paraphrase)
   ├── keyword  BM25 over key + value               (matches literal terms)
   └── fused    reciprocal rank fusion, scope-filtered, provenance attached
```

Either half may be missing -- an unavailable embedding model leaves keyword-only, and entries
written before embeddings existed still rank by keyword. Search reports which methods actually ran
so the caller can be honest about it.

---

## Agent definition

```yaml
id: research
name: Research Agent
description: Gathers and synthesises information.       # shown to the planner
system_prompt: |
  You are the Research Agent...
tools: [web.fetch, web.search, memory.read]             # allowlist
capabilities: [READ]                                     # grants
model_preference: deep                                   # fast -> Groq, deep -> Gemini
budget: {max_tokens: 40000, max_seconds: 120, max_tool_calls: 12}
```

Validated at boot: unknown tools, unknown capabilities, and **tools whose capability the agent was
not granted** all fail loudly at startup rather than mid-mission.

---

## Permission model

| Risk | Examples | Behaviour |
|---|---|---|
| `SAFE` | `web.fetch`, `memory.read` | runs |
| `SENSITIVE` | `memory.write`, `fs.write`, `android.tap` | runs if granted; audited |
| `DANGEROUS` | `memory.forget`, `shell.sudo`, `fs.delete` | **always parks for approval** |

Capabilities: `READ`, `WRITE`, `EXECUTE`, `FINANCIAL`.

Both gates must pass — the agent's tool allowlist *and* its capability grants. An agent is never
shown a tool it cannot call, which prevents most violations before they occur.

---

## Durability

The 24/7 requirement, made concrete:

| Mechanism | Failure it survives |
|---|---|
| Write every transition immediately | process killed mid-mission |
| **Leases**, not assignments | runner dies holding a task |
| **Boot reconciliation** | crash, restart, redeploy |
| **Idempotency keys** | retry re-running completed work |
| Budgets checked pre-dispatch | runaway loop draining quota |
| Bounded queues, capped buffers | memory growth over weeks |
| Per-mission context scope | one mission's context leaking into another |

On startup, before serving traffic: tasks in `RUNNING` whose lease has lapsed (or that never got
one) return to `READY`; mid-flight missions resume from their task graph.

---

## Statuses

```
Mission   PLANNING → RUNNING → { COMPLETED | FAILED | CANCELLED }
                        ↕
              WAITING · NEEDS_APPROVAL

Task      PENDING → READY → RUNNING → { SUCCESS | FAILED | SKIPPED | CANCELLED }
                               ↕
                        NEEDS_APPROVAL
```

`SKIPPED` means a dependency did not succeed — the task never ran, rather than running on bad input.

---

## Node protocol

Nodes dial out; nothing dials in (D-014).

```
node                                     orchestrator
 │── connect + hello ─────────────────────►│   enrolment token, or node_id + secret
 │◄── welcome ─────────────────────────────│   secret issued once, on first enrolment
 │── heartbeat + metrics (every 30s) ─────►│
 │◄── dispatch {request_id, tool, args} ───│
 │── result {request_id, ok, output} ─────►│
```

Authentication happens in the first *message*, not the handshake, because a node agent cannot set
headers reliably across platforms. Secrets are stored hashed; a leaked database does not let someone
impersonate a node.

**Placement is by capability, never hostname** (D-023):

| Capability | Meaning |
|---|---|
| `EXECUTE` | can run commands and touch files |
| `DESKTOP` | has a graphical session — screenshots, AT-SPI, input injection |
| `INFERENCE` | can run a local model |
| `STORAGE` | has disk to spare |
| `REPORT` | telemetry only — what an iPhone sends (D-013) |
| `ANDROID` | the Android companion app |

`gateway.select(capability, untrusted=...)` returns an idle node with that capability, preferring the
largest, or **None**. None means the tool is refused, not run somewhere else.

### What the node enforces itself

These live on the node, so they hold even if the orchestrator is compromised:

- **path jail** — every path is resolved *first*, then checked against the configured roots, so
  `../` cannot escape;
- **shell allowlist** — the command's first word must be permitted;
- **untrusted opt-in** — a node runs model-generated code only if explicitly cleared.

Reconnect uses exponential backoff (2s → 60s). At orchestrator boot every node is marked offline and
must re-announce itself, so the fleet view reflects reality rather than pre-crash state.

---

## Deployment

```
iPhone / any browser
   │  HTTPS · Tailscale Funnel (stable *.ts.net, free, no domain, no card)
   ▼
Host laptop — Debian 13 + XFCE · i5-1235U · 16 GB
   ├── Jarvis orchestrator (FastAPI + PWA, single origin)
   ├── PC node agent
   ├── Postgres (local)
   └── Ollama (later) — local offline fallback engine
        │
        └── cloud inference: Groq (fast) · Gemini (deep)
```

The laptop is primary. Other devices join as nodes using the identical protocol — the gaming laptop,
when it returns, is simply the largest one.
