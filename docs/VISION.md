# Vision — a personal Jarvis

The north star. [`PLAN.md`](PLAN.md) is the next few weeks; [`ROADMAP.md`](ROADMAP.md) is the long
list. This file is the reason both exist.

---

## What is being built

> An AI that is mine. It knows my history, my projects, my moods and how I like things done. It can
> do on my machines what I can do, and what AI can do on top. It gets better the longer I use it.

A multi-year project, and a hobby, and the way to learn AI/ML properly by building it.

---

## On "consciousness" — said plainly, once

Claude and ChatGPT are not conscious, and they don't know you between conversations by nature. Their
"memory" features are notes the system saves and pastes into the next prompt. Nobody has built a
conscious machine, and this project won't either.

What it *can* build is everything that makes an assistant feel like someone rather than something.
That comes down to seven properties, and every one of them is engineering:

| # | Property | Means | Feels like |
|---|---|---|---|
| 1 | **Continuity** | remembers across days and years, not just one chat | "it knows what happened last week" |
| 2 | **Character** | a stable personality and values, written down and editable | "it's the same Jarvis every time" |
| 3 | **Attunement** | notices your mood, energy and stress, and adjusts | "it can tell I'm tired" |
| 4 | **Initiative** | reaches out first — reminders, alerts, a morning brief | "it looks out for me" |
| 5 | **Reach** | wherever you are: phone, laptop, voice | "it's always there" |
| 6 | **Hands** | does real work on real machines, asking before anything risky | "it gets things done" |
| 7 | **Growth** | learns your procedures, eventually a model trained on you | "it's getting better at being mine" |

One rule follows from invariant 9 (never fabricate): **Jarvis tracks your feelings; it does not
claim its own.** A consistent character, yes. Pretending to be sad, no. An assistant that performs
emotions it doesn't have is lying to you, and the lie ends up in the training data it learns from.

---

## Where each property stands

| Property | Already built | Missing |
|---|---|---|
| Continuity | memory store with provenance, hybrid search, telemetry of every turn | memory written *automatically* from conversations; a diary that consolidates days into weeks |
| Character | — | a persona file: who you are, how Jarvis speaks, what it never does |
| Attunement | mood check-ins with a trend | mood used *in* replies; noticing mood from how you write |
| Initiative | todo reminders (not yet scheduled) | the schedule; watchers; a daily brief |
| Reach | PWA, MCP for Claude | **Telegram inbound** (started), then voice |
| Hands | missions, agents, permissioned tools, node fleet, approvals | approvals on the phone; mail, calendar; more machines |
| Growth | trajectories + thumbs, ShareGPT export | skills from repeated missions; a LoRA trained on your data |

The substrate — orchestration, permissions, durability, memory, nodes — is the hard part, and it's
done. What's missing is mostly in the top four rows. Those are what make it feel alive.

---

## Horizons

Rough, because years-long plans are guesses. Each horizon is entered only when the one before is used
daily — usage is what reveals which next step actually matters.

### Horizon 1 — Alive (now → early 2027)

Jarvis talks to you every day and remembers it.

- Telegram: chat, commands, approvals (P1–P2 in `PLAN.md`)
- schedule and watchers — it reaches out first (P3)
- **persona file** — character, written by you, versioned in git
- **automatic memory** — after each conversation, extract facts worth keeping, with provenance,
  reviewable and deletable (invariant 6)
- mood in context — today's check-in and the trend shape how it answers
- morning brief: one todo, what broke overnight, the day's mood check-in

*You learn:* LLM prompting for extraction, async systems, scheduling, retrieval quality.

### Horizon 2 — Capable (2027)

Jarvis does real work across your life and machines.

- mail and calendar: read, draft, send under approval
- the i3 and C340 as worker nodes; the Z Flip as a phone node
- **skills** — a mission that worked gets saved and re-run by name
- **episodic memory** — daily diary entries consolidated into weekly and monthly summaries, so years
  of history fit in a prompt
- voice: Whisper in, TTS out, on the phone first
- an evaluation set: 100 real questions with known good answers, so "it got better" is measured

*You learn:* evaluation, speech models, agent reliability, distributed systems.

### Horizon 3 — Yours (2028 →)

A model that is shaped by you, running on hardware you own.

- curate the telemetry: the turns you rated well
- LoRA fine-tune an open 7–14B model on a rented GPU, a few dollars a run
- serve it locally; cloud models stay for hard reasoning
- vision: it can look at a screen or a photo and act on it
- repeat every few months as the dataset grows

*You learn:* fine-tuning, quantisation, data curation, inference serving — the core of an ML career.

Training a frontier model from scratch is not on this list. It costs millions. A fine-tune of an open
model on your own conversations is what "my own LLM" means at personal scale, and it compounds with
everything above.

---

## What never changes

- **You own everything.** Your data stays on your machines (D-022). Cloud models are called; they
  don't store.
- **Every memory is inspectable, correctable, deletable** (invariant 6). Something that knows you
  this well must be something you can audit.
- **Risky actions ask first** (invariant 3). More capability means more reasons, not fewer, to keep
  the gate.
- **It says "I don't know"** (invariant 9).
- **One step at a time.** The fastest way to never have a Jarvis is to build all seven properties at
  once.
