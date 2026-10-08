# Jarvis / dhruv-ai — Restart Plan & Research Insights

Status: project was on hold after Phases 1–3 core landed. This document
reconciles **what the repo actually is**, **what the old roadmap promised**,
and **what a Jarvis-like personal AI needs in 2026** — then proposes a
restart sequence that protects the hard-won substrate without chasing
impossible scope.

Read with [`ARCHITECTURE.md`](ARCHITECTURE.md), [`DECISIONS.md`](DECISIONS.md),
and [`ROADMAP.md`](ROADMAP.md). This does not replace those files; it
re-aims them.

---

## 1 · Verdict in one paragraph

You already built the hardest part of a personal Jarvis: a **durable
multi-agent orchestrator** with permissions, approvals, budgets, crash
recovery, hybrid memory, a node fleet protocol, and a training-data flywheel.
What is missing is not "more architecture" — it is **presence** (Telegram /
voice), **everyday tools** (mail, calendar, laptop actions that feel fluid),
a **skills layer** (reusable procedures the system invents and keeps), and an
honest **local-LLM path** (Ollama for inference; cloud GPU for LoRA; not
"train Claude from scratch"). Resume this repo. Do not restart from zero.
Do not absorb OpenClaw or OpenJarvis wholesale — steal their channel and
skills patterns into *your* invariants.

---

## 2 · What the codebase actually is today

Verified against the tree and `pytest` on 2026-10-08: **176 tests passing**
(docs still say 150 — stale).

### Built and solid

| Layer | What exists | Evidence |
|---|---|---|
| Chat fast path | Intent router → Groq / Gemini / echo | `app/agent/` |
| Missions | Planner → task graph → scheduler, leases, boot reconcile | `app/missions/` |
| Agents | YAML specialists + one `AgentRunner` | `app/agents/definitions/*.yaml` |
| Tools | Capability + risk + approval gate | `app/tools/` |
| Memory | Hybrid vector+BM25, provenance, PWA panel | `app/memory/` |
| Nodes | `/ws/node`, enrolment, capability placement, PC agent | `app/nodes/`, `nodes/pc/` |
| Node tools | `fs.*`, `shell.exec`, `system.stats` (jailed) | `app/tools/node/`, `nodes/pc/agent.py` |
| Telemetry flywheel | Trajectories → ShareGPT JSONL export | `app/core/telemetry.py` |
| Todos + MCP | WIP-limited tracks, Claude MCP bridge | `app/todos/`, `mcp_server.py` |
| PWA | Chat, mood, missions, approvals, memory, logs | `app/static/` |

Agents shipped: `planner`, `research`, `memory`, `assistant`, `operator`.

### Claimed or half-built — do not treat as done

| Item | Reality |
|---|---|
| Phase 3 "done" in ROADMAP | **Protocol + PC agent done.** AT-SPI, ydotool, screenshot tool, Files/System/Terminal panels, Ansible playbook — **not in repo**. |
| Telegram "bot already exists" | Only `app/todos/reminders.py` outbound `sendMessage`. No inbound chat, no inline approval keyboards. |
| Phase 2.5 (devices, Telegram, fleet heatmap) | Marked "next"; **no implementation**. |
| Ollama / offline brain | Node *advertises* `INFERENCE` if `ollama` is on PATH. **`EngineRouter` has no Ollama engine** — only Groq, Gemini, echo. |
| Desktop control | `DESKTOP` capability is declared; no screenshot / AT-SPI / input tools. |
| Email / calendar | Absent. |
| Runtime tool / skill creation | Absent. Tools are `@tool` at develop time; agents are YAML. |
| Voice | Phase 6 on paper only. |
| Android companion | Phase 4 on paper only. |
| Personal LLM training loop | Export exists; no train/serve pipeline wired. |

### Doc debt that will confuse restarts

- `ROADMAP.md` marks Phase 3 ✅ then repeats an older Phase 3 block; Phase 8
  "Trading" body duplicates Phase 6 voice/Ollama text.
- `README.md` roadmap table contradicts itself (Phase 3 both done and empty).
- Test count and "Telegram bot exists" language are stale.

Fix the docs as you restart — stale status is how projects stay on hold.

---

## 3 · Your north-star, translated into engineering

> *Jarvis that researches itself, creates reusable tools/skills, does everyday
> tasks (mail → laptop), and can grow toward a personal LLM — hobby + learning.*

Map that to four product surfaces:

| Desire | Mechanically means | Fits existing design? |
|---|---|---|
| "Do everything" | More **tools** behind the permission boundary; more **channels** to reach you | Yes — extend tools + Phase 2.5 |
| "Research on itself" | Scheduled missions + memory consolidation + honest tool failures | Partially planned (Phases 5.5–6) |
| "Create new tools and skills" | A **skills** layer (procedures) + gated **tool authoring**, not free Python eval | Needs a new decision (below) |
| "Personal LLM like Claude" | (a) local inference, (b) LoRA on your traces, (c) pretrain frontier model | (a)(b) yes; (c) no as hobby |

The dangerous misread is treating (c) as the near-term goal. Claude-scale
pretraining is a lab with racks of GPUs. Your repo already planted the
realistic path: **orchestrate well + collect high-quality traces + LoRA a
7–13B model that sounds like you and knows your tools**.

---

## 4 · Deep research: what the field looks like in 2026

### 4.1 OpenClaw — channel-first personal assistant

Self-hosted gateway that meets you in WhatsApp / Telegram / Slack / Discord /
Signal, with nodes for device actions and a large community skill ecosystem
([docs](https://docs.openclaw.ai/concepts/architecture)).

| Steal | Leave |
|---|---|
| Messaging as primary UI, not a bolted bot | Replacing your mission scheduler |
| Skills as portable folders of instructions | Blindly importing 10k community skills without permission review |
| Device pairing + idempotency on side effects | Multi-tenant / team gateway complexity |

**Insight:** OpenClaw wins on *presence*. Your repo wins on *control plane*
(leases, budgets, DANGEROUS approvals, dual-dialect durability). The restart
should make Telegram feel as first-class as OpenClaw's gateway **without**
abandoning D-007 / D-008 / D-010.

### 4.2 OpenJarvis — local-first research stack

Five primitives: Intelligence, Engine, Agents, Memory, Learning; skills as
`SKILL.md` / `skill.toml`; Ollama/vLLM/llama.cpp backends; trace-driven
routing ([architecture](https://open-jarvis.github.io/OpenJarvis/architecture/overview/)).

| Steal | Leave |
|---|---|
| Skills as cross-cutting reusable workflows | Agent class hierarchies (you correctly chose YAML + one runner — D-006) |
| Engine ABC with Ollama as first-class | Replacing your hybrid memory (you already have RRF + FastEmbed — D-021) |
| Trace → learning loop | Research-grade GRPO router before daily usefulness |

**Insight:** You already have the Learning *data* half (telemetry + ShareGPT).
You are missing the Engine *local* half (Ollama) and the Skills half.

### 4.3 Agent Skills (Anthropic / OpenAI / agentskills.io)

Industry consensus in 2026: a **skill is a directory** with `SKILL.md`
(metadata + instructions + optional scripts), discovered at runtime, loaded
progressively so the prompt only sees name/description until invoked
([Anthropic](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills),
[OpenAI Skills](https://developers.openai.com/api/docs/guides/tools-skills)).

This maps almost exactly onto your planned **Phase 5.6 procedural memory**
("morning brief becomes a saved mission template") — but skills are the
portable *unit*, and templates are one implementation.

### 4.4 Self-evolving skill research (MUSE-Autoskill et al.)

Papers now treat skill creation as a lifecycle: create from runtime context →
execute → evaluate with tests → refine → store per-skill experience
([arXiv:2605.27366](https://arxiv.org/abs/2605.27366)). Voyager-style
"write code that becomes a tool" works in sandboxes; on a real laptop it is
how you brick a machine unless it sits behind your approval gate.

**Decision for this project:** agents may *propose* skills (markdown + tool
graphs). Installing a skill that adds a new primitive tool or shell capability
is always `DANGEROUS` + human approval. Skills that only compose already-granted
tools can be softer (SENSITIVE / audited).

### 4.5 Personal / hobby LLM reality (hardware-aware)

Your host plan: **i5-1235U, 16 GB, Iris Xe** (no discrete NVIDIA in the
architecture docs).

| Workload | Feasible? | How |
|---|---|---|
| Chat inference 7B Q4 (Qwen2.5 / Llama) | Yes | Ollama on host or i3/12 GB inference node |
| 13B Q4 inference | Tight on 16 GB | Prefer dedicated inference node |
| QLoRA fine-tune 7–8B | Not on this laptop | Rent 16–24 GB GPU (RunPod / Vast.ai), ~\$10–50/run |
| Train "full size like Claude" from scratch | No | Multi-million-dollar / multi-year; out of scope |
| Style / tool-use LoRA from your ShareGPT export | Yes — this is the flywheel | Unsloth or Axolotl → GGUF → Ollama Modelfile |

Your telemetry already excludes thumbs-down turns from export. That is the
correct seed for a personal model. Aim for **500–2000 high-quality preferred
turns** before the first LoRA, not "we need a frontier base model first."

---

## 5 · Previous plan vs proposed plan

### What the previous plan got right (keep)

1. **Four-layer separation** — orchestration / agents / tools / nodes.
2. **Invariants** — agents never call agents; permissions at tool boundary;
   DANGEROUS always parks; nothing important only in RAM; honest degradation.
3. **Capability-based fleet** (D-023) instead of hardcoded IPs.
4. **Devices dial out** (D-014); iPhone report-only (D-013).
5. **Telemetry as training data from day one.**
6. **Phased independence** — each phase valuable without waiting on hardware.
7. **Telegram as approval channel** (D-019) — still the right call for iPhone HITL.

### Where the previous plan drifted or over-promised

1. **Phase labeling outran reality** — Phase 3 ✅ while desktop GUI control and
   provisioning were still aspirational.
2. **Trading engine and JarvisOS** are late packaging / high-risk; they
   psychologically inflate "not done yet" and encourage holding.
3. **"From scratch" tension** — D-022 correctly reframed this as logic vs
   plumbing; the north-star still sometimes reads as rebuild everything.
4. **No explicit skills system** — "create tools" was left as developer work
   (`@tool` + YAML), which cannot satisfy the hobby vision of a self-improving
   assistant.
5. **Local LLM slotted in Phase 6** — too late. Offline brain is a *restart*
   priority, not a voice-era nice-to-have.
6. **Everyday life tools (mail, calendar) never appeared** on the roadmap at all.

### Proposed reframe: three horizons

```
Horizon A — Living assistant (weeks of focused work)
  Run on host · Ollama wired · Telegram chat+approvals · mail tools ·
  skills v0 (save successful missions as reusable procedures)

Horizon B — Embodied assistant (after A is daily-driven)
  Desktop control (screenshot → AT-SPI/ydotool) · episodic consolidation ·
  Android node · scheduled briefs · Files/System panels

Horizon C — Personal model + packaging (after real usage data)
  LoRA from telemetry · optional second inference node · JarvisOS ISO ·
  trading only if risk machinery is battle-tested
```

Trading, bootable ISO, and "Claude from scratch" move to Horizon C or
"conditional / research." They stop blocking the feeling of having Jarvis.

---

## 6 · Proposed architecture additions (aligned with invariants)

### 6.1 Skills layer (new — sits across agents/tools/memory)

```
skills/
  morning-brief/
    SKILL.md          # name, description, when to use
    graph.yaml        # optional: frozen task graph / tool sequence
  reply-important-mail/
    SKILL.md
```

- Discovery injects a short catalog into the planner / assistant prompt.
- Invocation loads full instructions (progressive disclosure).
- Successful missions can be *promoted* to skills (Phase 5.6, brought forward).
- Skill install / edit that widens capabilities → approval gate.

This is the missing answer to "create new tools and skills for reusing."

### 6.2 Primitive tools vs skills (keep the distinction forever)

| | Primitive tool | Skill |
|---|---|---|
| Example | `mail.send`, `fs.write` | "Weekly status email to manager" |
| Who authors | You (code) | You or Jarvis (markdown + graph) |
| Permission | Capability + risk | Inherits tools it calls |
| Runtime create? | Only via approved PR-like flow | Yes, with inspection |

Letting the model `@tool`-register arbitrary Python at runtime would break
D-008. Skills compose; primitives stay reviewed.

### 6.3 Ollama in `EngineRouter`

Add `OllamaEngine` behind the same `chat()` / `complete()` interface, prefer
it when cloud keys are missing or when `prefer=local`. Keep echo as the last
resort. Node `INFERENCE` capability then becomes placement for *heavy* local
jobs later — day one can run Ollama on the host.

### 6.4 Everyday tool packs (Horizon A)

Priority order for "does my life":

1. **Telegram** inbound + approval buttons (Phase 2.5 core)
2. **Mail** — IMAP read + SMTP draft/send (`DANGEROUS` send)
3. **Calendar** — read + create events (SENSITIVE)
4. **Browser / desktop** — after A is stable (Horizon B)

Mail before Android. Mail before vision. Mail before trading.

### 6.5 Personal LLM loop (Horizon C, seeded now)

```
use Jarvis daily
  → trajectories + thumbs (±1)
  → ShareGPT JSONL export (already exists)
  → curate / filter
  → Unsloth QLoRA on rented GPU (Qwen2.5-7B or 14B)
  → export GGUF → Ollama Modelfile `dhruv-ai`
  → EngineRouter prefers `dhruv-ai` for chat; cloud for hard reasoning
```

Repeat every few months as the dataset grows. That *is* "creating our own
LLM" in a way that compounds with this architecture.

---

## 7 · Concrete restart sequence

### Week-zero checklist (unblocks everything)

1. Bring up host per [`SETUP-HOST.md`](SETUP-HOST.md) (or confirm it still runs).
2. `pip install -r requirements-dev.txt && JARVIS_PASSCODE=dev uvicorn app.main:app`
3. Set `GROQ_API_KEY` and/or `GEMINI_API_KEY`; confirm `/health` is not degraded.
4. Enrol the host as a PC node (`SETUP-NODE.md`); ask a mission that uses
   `operator` + `fs.list` — prove the fleet path.
5. Fix doc status drift (test count, Phase 3 honesty, Telegram wording).

### Horizon A — become usable daily

| Order | Work | Why first |
|---|---|---|
| A1 | Wire **Ollama** into `EngineRouter` | Offline brain; hobby learning; no cloud required |
| A2 | **Telegram bot**: chat → `/command` or missions; **inline approve/deny** | Presence + HITL on iPhone (D-019) |
| A3 | iPhone Shortcuts → `/api/v1/devices/checkin` | Cheap telemetry; completes Phase 2.5 |
| A4 | **Mail tools** (IMAP/SMTP) + `assistant`/`operator` grants | Everyday task #1 people expect from Jarvis |
| A5 | **Skills v0**: save mission graph as `SKILL.md` + replay | "Create and reuse" without breaking permissions |
| A6 | Background scheduler (morning brief using skills) | Presence without you opening the PWA |

### Horizon B — embody on the laptop / phone

| Order | Work |
|---|---|
| B1 | `screenshot` tool + object storage (D-017) |
| B2 | AT-SPI read + ydotool inject (Wayland-aware) |
| B3 | PWA Files / System / Fleet panels |
| B4 | Episodic consolidation + supersession (Phase 5.5) |
| B5 | Android companion (Phase 4) when you want phone execution |
| B6 | Voice wake word + earpiece (Phase 6) once text habit is strong |

### Horizon C — personal model & packaging

| Order | Work |
|---|---|
| C1 | Dataset hygiene UI (browse/export/filter trajectories) |
| C2 | Documented Unsloth job + Ollama Modelfile for `dhruv-ai` |
| C3 | Optional second node for inference (i3/12 GB plan) |
| C4 | Ansible → live-build ISO (Phase 7) only after A+B stable |
| C5 | Trading (Phase 8) only after kill switch + budgets proven in anger |

---

## 8 · What not to do on restart

- **Do not rewrite in LangGraph / CrewAI / AutoGen.** D-020 still holds; your
  persistence and leases are the product.
- **Do not chase "full Claude from scratch."** Use LoRA on open weights.
- **Do not let the model install unrestricted shell tools.** Skills compose;
  primitives stay gated.
- **Do not start with trading or JarvisOS.** They are packaging / money risk.
- **Do not replace this repo with OpenClaw.** Integrate channel/skills ideas;
  keep your control plane.
- **Do not expand scope while the host is not running 24/7.** A suspended
  laptop is an offline Jarvis (SETUP-HOST lid rules).

---

## 9 · Success criteria (so "done" is falsifiable)

Horizon A is achieved when *all* of these are true:

1. You message Telegram and get a real Jarvis reply (not only reminders).
2. A `DANGEROUS` action can be approved from Telegram inline buttons.
3. With cloud keys removed, Ollama still answers (echo is last resort only).
4. Jarvis can draft/send email under approval, using a real mailbox.
5. At least one skill created from a successful mission can be replayed by name.
6. `pytest -q` still green on SQLite (and Postgres before storage changes).

Horizon B/C criteria stay as in ROADMAP, but A is the restart finish line.

---

## 10 · Suggested next decision log entries

When implementation starts, record:

- **D-026** · Skills are progressive-disclosure folders; capability-widening
  installs always require approval.
- **D-027** · Ollama is a first-class engine in the fallback chain; echo remains
  the terminal fallback.
- **D-028** · Mail send is `DANGEROUS`; mail read is `SENSITIVE`.
- **D-029** · Personal model training happens off-box; the host only serves
  GGUF via Ollama unless a GPU node advertises `INFERENCE` with CUDA/ROCm.

---

## 11 · Bottom line

The hold was not because the idea failed. The substrate is unusually strong for
a personal project. The gap is between **infrastructure completeness** and
**assistant aliveness**. Close that gap with Telegram + Ollama + mail + skills
before adding more phases. Treat "our own LLM" as a **fine-tune of an open
model on your telemetry**, looping every few months — that is both the learning
project and the path to something that feels like yours.
