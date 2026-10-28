# Jarvis for iPhone

A native client for Jarvis. It does what the PWA can't: Siri, Shortcuts automations, the Action
button, Face ID, and reading the phone's own state.

> **Not build-verified.** Written in a Linux container with no Xcode and no Swift toolchain. The
> Face ID, Keychain and App Intent code is adapted from Orbit and the crypto app, which do build and
> run on your iPhone. The models decode JSON captured from the real API, and the server side of the
> contract is pinned by `tests/test_ios_contract.py`. Still, expect a few small compile fixes on the
> first `xcodebuild` — a renamed API or an availability mismatch, not a design problem.

---

## Before you build: the three-app limit

Your Apple account is a **free Personal Team** (`3Y62758W54`, same as Orbit). Two limits matter:

| Limit | What it means |
|---|---|
| **7 days** | the install expires weekly. `scripts/reinstall.sh` puts it back. |
| **3 free-signed apps on the phone at once** | **app extensions count.** Orbit + Orbit's widget + the crypto "dhruv" app is already three. |

So installing Jarvis will probably fail with *"maximum number of apps for free development
profiles"*. Pick one:

1. **Delete one of the others from the phone.** Orbit is parked — its widget alone frees a slot.
2. **Join the Apple Developer Program** (US$99/year). Removes both limits, and is the only way to get
   remote push notifications, widgets with shared data, and TestFlight.

Jarvis itself has **no extensions** on purpose — Siri and Shortcuts work through App Intents inside
the app, which cost nothing extra.

---

## Build and install

On the Mac, with the iPhone plugged in and unlocked:

```bash
cd ios
xcodegen                     # generates Jarvis.xcodeproj (gitignored)
./scripts/reinstall.sh       # build, sign, install, launch
```

Or open `Jarvis.xcodeproj`, pick your iPhone, and Run. Tests run on the simulator:

```bash
xcodebuild test -project Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 16'
```

First launch shows Settings. Enter:

- **Server** — your Funnel URL (`https://dhruv-ai.<tailnet>.ts.net`), or `dhruv-ai:8000` over
  Tailscale while Funnel is off
- **Passcode** — `JARVIS_PASSCODE` from the host's `.env`

*Save and test* checks both against the server **before** saving, so a typo can't overwrite a
working setup.

---

## What's in it

| Screen | Does |
|---|---|
| **Today** | the one next todo, mark it done, a five-face mood check-in |
| **Chat** | talk to Jarvis — same conversation engine as Telegram and the PWA |
| **Todos** | everything, grouped by track; active first, parked dimmed; swipe to finish, `+` to capture |
| **Approvals** | dangerous actions waiting for you. **Approve always asks for Face ID**; Deny never does |
| **Settings** | server, this phone's name, app lock, Siri phrases |

### Siri and Shortcuts

| Say | Does | Phone must be unlocked? |
|---|---|---|
| "Hey Siri, ask Jarvis" | asks a question, speaks the answer | yes — the answer can quote your memory |
| "What's next in Jarvis" | the one next todo | yes |
| "Add a todo to Jarvis" | files a todo | no — writing reveals nothing |
| "Log my mood in Jarvis" | mood check-in | yes |
| "Report to Jarvis" | sends this phone's state | no — automations run while it's locked |

All five also appear in the Shortcuts app as actions, and any of them can go on the Action button.

### Automations — the phone as Jarvis's senses

A free-team app can't run in the background on its own. **Shortcuts automations can**, and they
trigger on things the app could never see. Each one runs *Report to Jarvis* with a reason:

| Shortcuts › Automation › | Reason to type | What Jarvis learns |
|---|---|---|
| Arrive at *office* | `arrived_office` | you're at work — and when |
| Leave *home* | `left_home` | the day has started |
| Charger connected | `charging` | usually: desk or bed |
| Time of day, 23:00 | `night` | battery and screen state at night |
| Focus › Sleep turns on | `sleep_focus` | when you actually go to bed |

Turn off **Ask Before Running** on each. The location triggers use Shortcuts' own permission — the
Jarvis app never asks for your location.

### What gets sent

Only what needs no permission: battery level and state, Low Power Mode, thermal state, uptime, time
zone, free storage, network type (Wi-Fi, cellular, Low Data Mode). Opening the app sends a report at
most every 30 minutes; automations and *Report now* always send.

Every report goes only to your host, is kept with what triggered it, and can be wiped:
`DELETE /api/v1/devices/<device_id>`.

---

## What it can't do, and why

- **Execute anything.** iOS forbids one app from controlling another (D-013). The iPhone reports and
  approves; Android and the laptops execute.
- **Push notifications.** Remote push needs the paid program. Telegram stays the channel that reaches
  you first — the app is where you look when you want to.
- **Run in the background on a schedule.** Use automations; they're more reliable anyway.

---

## The access ladder — what to add next

Each rung is one permission, added on its own, with a reason for Jarvis to have it. Never a bundle.

| Rung | Permission | Gives Jarvis | Notes |
|---|---|---|---|
| ✅ 0 | none | battery, power, network, storage, time zone | this build |
| 1 | **Calendar** (EventKit, read) | today's meetings — for the morning brief | Info.plist string only; no entitlement. Sees every account on the phone, so no Google OAuth on the server |
| 2 | **Health** (HealthKit, read) | sleep, steps, resting heart rate — *attunement* (VISION.md) | needs the HealthKit capability; confirm it signs on the free team before relying on it |
| 3 | **Reminders / Contacts** | who and what you mention | only if a real use appears |
| 4 | **Local notifications** | a nudge when an approval is waiting and the app has seen it | not push — only fires after the app has run |
| — | **Paid program** | remote push, widgets, TestFlight, year-long installs | a money decision, not a code one |

Location stays with Shortcuts' triggers rather than an app permission: the automations above give
the useful part (arrived, left) without the app ever tracking where you are.

---

## Layout

```
ios/
  project.yml              XcodeGen spec — the .xcodeproj is generated, not committed
  scripts/reinstall.sh     weekly rebuild for the free team
  Jarvis/
    JarvisApp.swift        entry: lock, connection, report on open
    Core/                  API client, models, Keychain, connection, device context
    Security/              Face ID gate + app lock (from Orbit)
    Features/              Today, Chat, Todos, Approvals, Settings
    Intents/               Siri + Shortcuts
  JarvisTests/             decoding (real captured JSON), encoding, URL rules
```

Same rules as the server: no third-party packages, comments explain *why*, and the app never
re-implements a rule the server owns — the two-track limit, the one-next-item rule and the approval
gate all live in Python, tested, once.
