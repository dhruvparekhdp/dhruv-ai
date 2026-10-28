# Plan — October 2026

The current plan. Supersedes the phase ordering in [`ROADMAP.md`](ROADMAP.md) where they disagree;
the roadmap stays as the long list of what Jarvis could become.

Written 18 Oct 2026 against `main` at `8ba215e` (176 tests). Status updated 19 Oct: P1 built,
iOS client written (P1b), **213 tests passing** on SQLite and Postgres 16.

The long-term direction is [`VISION.md`](VISION.md). This file is the next few weeks of it.

---

## 1 · The honest starting point

The substrate is done and solid. Missions, agents, tools, permissions, approvals, budgets, crash
recovery, hybrid memory, the node protocol, todos and the MCP bridge all exist and are tested.

What does not exist is a reason to open Jarvis every day. Nothing reaches you. Nothing watches
anything you care about. The todo reminders never fire: `POST /api/v1/todos/reminders/sweep`
exists, and nothing calls it.

Meanwhile the work happens elsewhere. Last push per project:

| Project | Last push | State |
|---|---|---|
| `crypto-signal-engine` | 18 Oct | live on EC2, 1,473 tests, daily commits |
| `dhruvparekh.in` (site + Saloni shop) | 17 Oct | live on Netlify, takes real orders |
| `secret-santa` (GiftHat) | 13 Oct | plan only, launch target 23 Nov |
| `dhruv-ai` (Jarvis) | 8 Oct | untouched since the restart audit |
| `ocr` | 16 Sep | stable |
| `dhruv-ios` (Orbit) | 6 Sep | parked by decision |

That is four active projects against a WIP limit of two — the limit `app/todos/` enforces in code.
The problem Jarvis was meant to solve is the problem Jarvis is currently losing to.

So the plan below does not add layers. It gives Jarvis **one job you would notice if it stopped**,
and builds only what that job needs.

---

## 2 · What Jarvis is for, now

> **The control plane for your own machines and projects, reached over Telegram.**

Three jobs, in priority order:

1. **Tell you what to work on.** One item, from the todo list, pushed to your phone. Already built;
   needs a channel and a timer.
2. **Watch what you've deployed.** The crypto engine, the Saloni shop, the site, the host itself.
   Tell you when something breaks, before a customer or a missed trade does.
3. **Do bounded chores on the fleet.** Through the existing tools, under the existing approval gate.

Everything in the old roadmap that does not serve one of these three — voice, Android, vision,
desktop control, the ISO, trading inside Jarvis — waits. Not cancelled. Waiting until Jarvis is used
daily, because until then nothing tells you which of them actually matters.

---

## 3 · Architecture

The four layers and nine invariants in [`CLAUDE.md`](../CLAUDE.md) do not change. Two new edges get
added around them: a **channel** (how you reach Jarvis) and **watchers** (how Jarvis reaches the
things you run).

```
        iPhone ─ Telegram ─┐                    ┌─ PWA (todos screen, later)
                           ▼                    ▼
                  ┌──────────────────────────────────────┐
  CHANNELS        │  telegram: commands · chat · approve │   new — app/channels/
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
  ORCHESTRATION   │  fast path (chat, todos)  │ missions │   unchanged
                  └──────────────────┬───────────────────┘
                                     │
  AGENTS / TOOLS  │  existing tools + http.check (SAFE)  │   one new tool
                                     │
                  ┌──────────────────▼───────────────────┐
  SCHEDULE        │  timers: reminder sweep · watchers   │   new — app/schedule/
                  └──────────────────┬───────────────────┘
                                     │
  WATCHED         crypto engine /health · /api/llm/budget
                  dhruvparekh.in · Saloni · host disk/RAM/temp
```

### 3.1 Channel: Telegram, inbound

- **A new bot, separate from the crypto engine's.** Telegram allows one `getUpdates` consumer per
  bot. Two programs polling the same token steal each other's messages. A second bot is free and
  takes a minute in BotFather. (D-029)
- **Long-polling, not webhooks.** Polling dials out, which keeps invariant 8 — nothing dials in —
  and works whether or not Funnel is up.
- **Only your chat ID is answered.** Everything else is dropped and logged. The passcode gate has
  no equivalent on Telegram, so this check is the gate.
- Commands map onto the fast path, never a mission: `/next`, `/add <track> <text>`, `/done <id>`,
  `/tracks`. Free text goes to chat. A mission is opt-in with `/do <goal>`.
- **Approvals as inline buttons.** A `DANGEROUS` tool already parks the task; the channel just
  renders the park as `[Approve] [Deny]` and posts the decision to the existing approval endpoint.
  No new permission logic — invariant 3 is enforced where it already is.

### 3.2 Schedule: one timer loop, durable

- A single asyncio loop started in `lifespan`, ticking once a minute.
- Jobs are rows, not code: `(name, interval, last_run_at, next_run_at)`. Last-run is written before
  the job's side effect, so a crash between "ran" and "recorded" re-runs at most once — and every
  job is required to be idempotent, like the reminder sweep already is.
- First two jobs: the reminder sweep, and the watchers.
- Until this lands, a systemd timer calling the sweep endpoint does the same job with zero code.

### 3.3 Watchers

- One new tool, `http.check(url, expect_status, expect_json_path?)` — `READ`, `SAFE`.
- A watcher is config, not code: name, URL, interval, what counts as healthy.
- Alert on **state change**, not on every failed check. Down → alert once. Up again → alert once.
  An alert channel that repeats itself gets muted, and a muted alert channel is worse than none.
- Every check result is a row, so "how often was the shop down this month" is a query.

### 3.4 LLM gateway: borrow from the crypto engine

The crypto engine hit Groq's free daily token limit about 240 times on 5–6 Oct and solved it with
per-model budgets (`collectors/llm_budget.py`): requests and tokens per minute and per day, held at
85% of the limit, `x-ratelimit-*` headers honoured, and a 429 cools a model down for exactly the
time the provider names.

Jarvis's `EngineRouter` falls through providers on *failure* only. It should also skip a provider
that is *about to* fail. Port the pattern — the code, not a runtime dependency; the trading engine
must never depend on a laptop at home.

Ollama: see open question 1.

### 3.5 What stays out, and why

| Not now | Why |
|---|---|
| Mail tools | Needs Gmail OAuth or app passwords, and `send` is `DANGEROUS`. Valuable, but it is a new trust boundary — do it once the approval channel is proven on Telegram. |
| Skills layer | The right idea (the 8 Oct audit's best point). But a skill is a *successful mission saved for reuse*, and there are no successful missions yet because nothing runs any. Earn it with usage first. |
| Desktop control, screenshots | The host is headless now (D-026). There is no desktop to control. |
| Android node | The Z Flip's role is a backup network link, not an execution node. |
| Trading inside Jarvis | Already has a home with its own risk machinery. Jarvis watches it; it does not run it. |

---

## 4 · Phases

Each phase ends with something you can check on your phone. Each is small enough to finish in one or
two evenings, because it has to fit around everything else on the list.

### P0 — Host baseline (one evening, mostly commands)

Done from the host's shell. Detailed steps: [`SETUP-HOST.md`](SETUP-HOST.md).

- SSH keys, password auth off, Cockpit, lid ignore, battery cap, `unattended-upgrades`, clock sync
- Tailscale; Jarvis as a systemd service with a real passcode
- systemd timer calling the reminder sweep every 5 minutes
- `fastembed` installed now that there's real network

**Done when:** `sudo reboot`, and two minutes later a due todo arrives on Telegram without you
touching anything.

### P1 — Telegram channel ✅ built, awaiting the on-phone check

`app/channels/telegram.py`: long-poll loop, chat-ID gate, `/next` `/add` `/done` `/tracks` `/help`,
free-text chat in one persistent session. At-most-once delivery: the cursor is recorded before each
update is handled. 23 tests, including the two that matter — a stranger changes nothing, and a
restart never replays an `/add`.

To switch it on: create a **new** bot in BotFather (D-029), and set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in the host's `.env`. Polling starts with the server.

**Done when:** from your phone, `/next` returns one item, `/add jarvis test` creates one, `/done`
closes it — and a message from any other account gets no reply.

### P1b — Native iOS app ✅ written, needs its first Mac build

`ios/` — added on request, 19 Oct (D-030). Today, chat, todos, approvals with Face ID, settings;
five Siri/Shortcuts intents; *Report to Jarvis* for automations. Server side:
`/api/v1/devices/checkin` with list, history and wipe; `tests/test_ios_contract.py` pins every
payload the app sends and every field it reads.

**Done when:** it installs on the iPhone (mind the three-app limit — `ios/README.md`), "Hey Siri,
what's next in Jarvis" answers, and an "arrive at office" automation leaves a report in
`GET /api/v1/devices`.

### P2 — Approvals on Telegram (one evening)

Render parked approvals as inline buttons; callbacks post to the existing approval API.

**Done when:** a mission that calls `fs.delete` parks, the buttons arrive on your phone, Deny leaves
the file in place, Approve removes it.

### P3 — Schedule + watchers (two evenings)

The durable timer loop replaces the systemd timer. `http.check`. Watchers for the crypto engine's
health and LLM budget, dhruvparekh.in, the Saloni shop, and host disk.

**Done when:** stop the crypto engine's service; one alert arrives. Start it; one recovery message.
No repeats in between.

### P4 — Todos in the PWA (two evenings) — lower priority now

The iOS app's Todos tab covers the phone. This is now only for a laptop browser, and can wait until
one is actually missed.

**Done when:** it works from a laptop browser over the Funnel URL.

### P5 — LLM budgets (one evening)

The per-model budget in `EngineRouter`, from §3.4.

**Done when:** a test that exhausts a fake provider's daily budget shows the router skipping it
*before* calling it, and resuming after the window.

### After P5

Decide from usage, not from this document. Likely candidates, in rough order: mail (read, then
draft, then send under approval), skills from missions that actually got re-run, the i3 and C340 as
worker nodes, episodic memory.

---

## 5 · Order versus your list

Your list had the todos PWA before any channel work. This plan put Telegram first, because a todo
list that can't push is a list you have to remember to open. The native app (P1b) replaced the PWA
screen for the phone; Telegram is still the push channel, because the free Apple team can't receive
remote push.

MCP wiring (your item 6) is a one-line `claude mcp add` once P0 gives the API a stable URL. It is in
P0, not a phase.

---

## 6 · Open questions

1. **Ollama as a fallback, not primary?** The decision was no local Ollama, because Groq is faster.
   That still holds for the primary path. But Ollama is already installed on the host for crypto
   research, and Groq's daily limit has been hit in anger. A 7B model is a better last resort than
   the echo engine, which can only say "no engine available". Cost: one engine class and ~5 GB of
   disk. Your call — the plan works either way.
2. ~~Which two tracks are active?~~ Resolved 19 Oct: GiftHat runs in its own conversation and is
   nearly done; this plan is Jarvis only.
3. **Mail provider** — Gmail, or something else? Only matters after P5.
4. **Three-app limit on the iPhone** — remove Orbit's widget or another app, or pay for the
   developer program? Blocks P1b's install, not its code. See `ios/README.md`.

---

## 7 · Relation to the 8 Oct restart audit

The audit on branch `cursor/restart-plan-insight-7c7b` was accurate about the code and is folded in
here: Phase 3 is core-only (no screenshot, AT-SPI, panels or Ansible), Telegram is outbound-only,
`EngineRouter` has no Ollama engine, the router supports only `PERSONAL_CHAT`, and the test count is
176. Its skills design (§3.5 here) is the right one when the time comes.

Where this plan differs: the audit put Ollama first, ahead of any channel. That contradicts the
decision on record and would deliver a better fallback for a system nobody is using yet. Presence
first, then resilience.
