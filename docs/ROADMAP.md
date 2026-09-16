# Roadmap

Each phase is independently valuable and testable. Phases are ordered so that nothing waits on
hardware that has not arrived.

---

## ✅ Phase 1 — Foundation

FastAPI gateway serving the API and PWA from one origin. Dual-engine LLM router (Groq / Gemini /
echo). Dataset-grade telemetry with ShareGPT JSONL export. Chat, mood check-ins with a trend strip,
thumbs-up/down reward capture. Passcode gate. Installable iPhone PWA. Live WebSocket stream.

**45 tests**, SQLite and Postgres.

---

## ✅ Phase 2 — Orchestration substrate

Missions and task graphs. Declarative agents (YAML) run by one generic `AgentRunner`. Tool registry
with capability + risk. Permission enforcement at the tool boundary. HITL approval gate. Budget and
depth guards. Audited message bus. Durability: leases, boot reconciliation, idempotency keys.
Mission Control panel with live progress and approval cards.

Agents: `planner`, `research`, `memory`, `assistant`.
Tools: `web.fetch`, `web.search`, `memory.read/write/forget`.

**86 tests**, SQLite and Postgres 16. Approval gate verified end-to-end in a browser.

---

## ✅ Phase 2.6 — Memory search (brain, part A)

Hybrid retrieval over memory: local embeddings (FastEmbed, ONNX, no PyTorch) fused with BM25 keyword
ranking via reciprocal rank fusion. `memory.search` tool for agents, plus a Memory panel in the PWA
to browse, search, correct and delete everything Jarvis believes.

Every entry carries **provenance** — when it was learned and which agent or mission wrote it. A
confidently recalled wrong fact is worse than no memory, so it must be inspectable.

Degrades honestly: if the embedding model is unavailable, search says "keyword only" rather than
silently returning worse results.

**111 tests**, SQLite and Postgres.

---

## ▶ Phase 2.5 — Device registry, iPhone telemetry, Telegram ← next

Deliberately parallel with Phase 3; needs no new hardware.

- `/api/v1/devices/checkin` for an **iOS Shortcuts** automation (battery, device, network, on Wi-Fi
  connect) — the only way to get iPhone telemetry without a Mac or jailbreak
- **Fleet heatmap** panel: device tiles colour-coded on health, GitHub-style activity grid per
  device, detail drawer
- **Telegram bot**: chat, voice messages, and — the real prize — **inline approval buttons**, which
  fixes HITL approval on the iPhone without web-push setup

---


## ✅ Phase 3 — Node protocol + first nodes

Turn the fleet into something real, with the count left open from the start.

**Node protocol**
- `/ws/node` gateway: enrolment tokens, node sessions, heartbeat, lease renewal
- **capability-based registry** (D-023) — nodes advertise `ram_mb`, `cores`, `capabilities`,
  `untrusted_ok`; the scheduler places work against those, never against hardcoded IPs
- node-tool dispatch: a tool marked `requires_node` routes to a node instead of running in-process
- reconnect with backoff, reporting current task so the orchestrator reconciles rather than duplicates
- offline node → its leases lapse → work reschedules automatically

**PC node agent** (`nodes/pc/`)
- `shell.exec` — allowlisted; `DANGEROUS` outside the allowlist
- `fs.list` / `fs.read` / `fs.write` / `fs.move` — jailed to configured roots
- `fs.delete` — `DANGEROUS`, always approval-gated
- `screenshot`, `system.stats` (CPU, RAM, disk, temperature, processes)
- `systemd` unit with restart-on-failure

**Desktop control via AT-SPI + ydotool** (from the updated plan)
- read the accessibility tree for *semantic* control — "click the button labelled Save" — instead of
  guessing pixels
- `ydotool` / `/dev/uinput` for real keystroke and click injection, which works under Wayland where
  X11-era tools do not
- caveat: AT-SPI is more mature on X11; `/dev/uinput` needs a udev rule

**System GUI in the PWA** — so the machine is operable without the terminal
- **Files panel** — browse, upload, download, rename, move, delete (deletes go through approval)
- **System panel** — CPU / RAM / disk / temperature, processes, service health, per node
- **Fleet panel** — every node, its capabilities, health, current task
- **Terminal panel** — for when a command is wanted, not required

**Host provisioning** — Ansible playbook: packages, users, firewall, `chrony` (D-025), Tailscale,
Postgres, node service, hardening. Reproducible, so a new machine joins in minutes. This playbook is
also the build recipe the ISO later consumes.

See [`SETUP-HOST.md`](SETUP-HOST.md).

---

## Phase 3.5 — Fleet hardening

Once more than one machine is running, these stop being optional:

- **Network segmentation** — the sandbox node must not reach the database node or the open internet
  freely. Firewall rules, or "isolated sandbox" is a label rather than a boundary.
- **Backups** — nightly `pg_dump` to a second node plus an external drive. One disk currently holds
  all memory and training data.
- **Centralised logs** — node logs ride the existing WebSocket bus; four machines cannot be tailed
  by hand.
- **Wake-on-LAN** — idle nodes sleep, the orchestrator wakes them for work. Cuts power, heat and fan
  noise to near zero when nothing is running.
- **Global kill switch** — one button halts every mission, revokes every node lease, stops dispatch.
  Broader than the trading-only version in the source plan.
- **Secrets across nodes** — `systemd` credentials or an age-encrypted file, not `.env` copied
  around.

Turn the host laptop into the first execution node, and give Jarvis the GUI that makes the machine
usable without a terminal.

**Node protocol**
- `/ws/node` gateway: enrolment tokens, node sessions, heartbeat, lease renewal
- device registry with `REPORT` / `EXECUTE` capabilities and health
- node-tool dispatch: a tool marked `requires_node` routes to a node instead of running in-process
- reconnect with backoff, reporting current task so the orchestrator reconciles

**PC node agent** (`nodes/pc/`)
- `shell.exec` — allowlisted, `DANGEROUS` outside the allowlist
- `fs.list` / `fs.read` / `fs.write` / `fs.move` — jailed to configured roots
- `fs.delete` — `DANGEROUS`, always approval-gated
- `screenshot`, `system.stats` (CPU, RAM, disk, temperature, processes)
- runs as a `systemd` unit with restart-on-failure

**System GUI in the PWA** — so the machine is operable without typing commands
- **Files panel** — browse, upload, download, rename, move, delete (deletes route through approval)
- **System panel** — CPU / RAM / disk / temperature, top processes, service health
- **Terminal panel** — for when a command is wanted, not required

**Host provisioning** — an Ansible playbook that turns bare Debian into a Jarvis host: packages,
users, firewall, Tailscale, Postgres, the node service, hardening. Reproducible, so a new machine is
a 20-minute rebuild rather than a weekend of recollection.

See [`SETUP-HOST.md`](SETUP-HOST.md).

---


## Phase 4 — Android companion

Kotlin app, sideloaded (Play Store rejects this class of app).

- foreground service + `AccessibilityService`, `RECEIVE_BOOT_COMPLETED`, battery-optimisation
  exemption
- same node protocol as the PC node
- `android.tap / swipe / type / launch / screenshot / notify`
- local Room queue so a task survives app restart

Testable without owning a phone: **Waydroid** or the Android Studio emulator, KVM-accelerated on the
host laptop.

---

## Phase 5 — Vision-verified automation

Vision Agent reading screenshots from S3-compatible object storage — never the database (D-017).

The act→verify loop that separates real automation from blind scripting:

```
tap "Login" → screenshot → "login screen confirmed" → tap → screenshot → "login succeeded"
```

Plus retention: prune screenshots after N days, compact agent messages once missions complete.

---

## Phase 5.5 — Episodic memory + consolidation (brain, part B)

The piece that makes memory a brain rather than a log. Without it memory either grows unbounded
until retrieval is noise, or stays empty because nobody writes to it.

- **Episodes** — a durable record of what happened and when, embedded and searchable, built from
  trajectories and mood check-ins that are already being logged but never read back.
- **Nightly consolidation** — a scheduled mission where a reflection agent reads the day's
  trajectories and writes durable facts into semantic memory plus a day summary into episodic
  memory. Raw trajectories stay for training but stop being the retrieval path.
- **Supersession** — "I live in Pune" followed later by "I moved to Bangalore" must resolve, not
  accumulate as two contradictory facts. Entries gain `superseded_by` (the column already exists)
  and consolidation reconciles conflicts.
- **Decay and pruning** — `access_count` and `accessed_at` (already recorded) drive relevance;
  stale mission scratch memory ages out.

Depends on Phase 6's background scheduler for the nightly run.

---

## Phase 5.6 — Procedural memory (brain, part C)

Memory of *how*, not just *what*. "Morning brief" becomes a saved mission template — the task graph
Jarvis worked out once, stored and replayable.

- successful mission graphs saved as reusable templates
- a matcher that recognises "do the thing I asked for last Tuesday"
- templates are editable and inspectable, like every other kind of memory

This is where the assistant stops re-deriving the same plan every time, and it is only worth
building once there are enough real missions to learn from.

---

## Phase 6 — Voice & presence

- Whisper STT, Piper/Edge-TTS, and a **local wake word** (openWakeWord) over PipeWire
- **Bluetooth earpiece** as an always-on voice channel — the thing that makes this feel like an
  assistant rather than a dashboard
- Ollama with `qwen2.5:7b-instruct` on the inference node as the offline fallback engine; the
  `EngineRouter` already has the slot
- scheduled background missions: morning brief, system health, backups

---

## Phase 7 — JarvisOS (bootable image)

A real milestone, deliberately late: an ISO is a *packaging* step, and it only makes sense once
there is a stable system to package.

- Debian `live-build` profile that bakes in the Ansible playbook from Phase 3
- Jarvis and the node agent pre-installed as `systemd` units, first-boot enrolment wizard
- AT-SPI, `ydotool` and `/dev/uinput` permissions configured by default
- output: a USB image that turns any spare laptop into a Jarvis node in one boot

Everything the playbook does is reused here — nothing built earlier is wasted.

---

## Phase 8 — Trading engine

Last, deliberately. Real money, so it lands only when the approval gate, kill switch, audit log and
budget machinery have all been proven by everything above.

- Whisper STT + TTS; wake word
- Ollama with `qwen2.5:7b-instruct` as the offline fallback engine — the `EngineRouter` already has
  the slot, and 16 GB makes it viable
- broaden to 10–15 agents (now just YAML files)
- scheduled background missions: morning brief, system health, backups

---

## Later / conditional

| Item | Blocked on |
|---|---|
| Autonomous coding agent | Phase 3 (needs `fs.*` + `shell.exec`) |
| Scratch-built KVM | monitors on the spare laptops (D-024) |
| Smartwatch node | a Wear OS watch; an Apple Watch needs a Mac + dev account |
| Diskless PXE boot | **declined** — mixed chipsets, >60% rework (source plan agrees) |
| pgvector migration | only if memory passes ~100k entries; brute force is fine below that |
| Trading engine + hard risk limits | real money — deliberately last |
| Discord bot for full-duplex voice | wanting live voice over voice messages |
| Telephony gateway | a paid number; interface only until then |
| JWT + WebAuthn passkeys | replacing the Phase 1 passcode |
| NixOS migration | Jarvis running stably first |
| Second always-on host | if laptop downtime becomes a problem |
