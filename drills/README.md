# Debugging drills

You get a **symptom**, not a cause. Find the bug, fix it, prove it with the tests.

Every drill is a real bug class from this codebase or its domain. **Three of them
actually shipped here** and had to be found the hard way — 01, 06 and 09.

```bash
python3 drills/run.py list          # what's available, and your progress
python3 drills/run.py start 01      # plant the bug
python3 drills/run.py check 01      # run the tests that should catch it
python3 drills/run.py hint 01       # one more nudge (repeatable)
python3 drills/run.py solve 01      # reveal and explain
python3 drills/run.py reset 01      # put the file back
```

**Rule of the game:** read `check` output before `hint`, and `hint` before `solve`.
The struggle is the part that teaches. If you jump to `solve`, you've read a fact
instead of building a skill.

---

## The drills

| ID | Title | Area | Difficulty | ~min |
|---|---|---|---|---|
| 01 | The chart draws backwards | python | easy | 20 |
| 02 | Dependent tasks never run | python | easy | 20 |
| 03 | Node commands hang forever | async | medium | 25 |
| 04 | A runaway agent burns the daily quota | backend | medium | 25 |
| 05 | One join token, many nodes | netsec | medium | 25 |
| 06 | The sandbox is not a sandbox | os-security | **hard** | 35 |
| 07 | Untrusted code runs on the wrong machine | os-security | medium | 25 |
| 08 | Dangerous tools stop asking permission | netsec | medium | 20 |
| 09 | Internal plumbing leaks to the node | backend | medium | 20 |
| 10 | The secret leaks through the clock | netsec | **hard** | 30 |

Suggested order: **01 → 02 → 03 → 09 → 04 → 05 → 08 → 07 → 06 → 10.**
That climbs from Python fundamentals through async, then backend, then security,
ending on the two that need the most patience.

---

## How to debug, as a method

Most people debug by guessing and editing. This is the loop that actually works,
and it is what the drills train:

```
   ┌─────────────────────────────────────────────────┐
   │  1. REPRODUCE   make it fail on demand          │
   │     ↓           (check <id> does this for you)  │
   │  2. READ        what does the failure SAY?      │
   │     ↓           expected X, got Y — take it     │
   │                 literally, not approximately    │
   │  3. LOCALISE    which line could produce Y?     │
   │     ↓           bisect: half the code is        │
   │                 innocent, prove which half      │
   │  4. HYPOTHESISE one sentence: "Y happens        │
   │     ↓           because Z"                      │
   │  5. TEST IT     change ONE thing. Does the      │
   │     ↓           failure change as predicted?    │
   │  6. FIX + PROVE test passes, others still pass  │
   └─────────────────────────────────────────────────┘
```

Two habits worth more than any tool:

**Read the error literally.** `assert 5 == 4` means the value really was 5. Not
"about 5", not "5 sometimes". Beginners skim errors; the error is usually telling
you the answer.

**Change one thing at a time.** Two simultaneous changes and a passing test tells
you nothing about which one mattered.

### Your instruments

```bash
python3 -m pytest -q                      # everything
python3 -m pytest -q path::test_name      # one test
python3 -m pytest -x -vv path::test_name  # stop at first failure, full detail
python3 -m pytest --pdb path::test_name   # drop into a debugger at the failure
```

Inside `pdb`: `p expr` prints, `l` lists code, `u`/`d` move up and down the stack,
`c` continues. `print()` is not cheating — it is the most-used debugger on earth.

---

## Where the drills point

| Area | Files | What you're learning |
|---|---|---|
| python | `core/telemetry.py`, `missions/scheduler.py` | `is` vs `==`, precision, sort stability |
| async | `nodes/gateway.py` | coroutines don't run until awaited |
| backend | `agents/runner.py`, `missions/store.py` | budgets, boundaries, state machines |
| netsec | `nodes/store.py`, `tools/registry.py` | credential lifecycle, timing attacks, gates |
| os-security | `nodes/pc/agent.py` | path traversal, privilege boundaries |

---

## If a drill leaves the repo dirty

`reset <id>` restores the file. To undo everything:

```bash
python3 drills/run.py reset-all
git diff                  # should be empty
```

Drills edit real source files, so commit or stash your own work first.
`drills/.state.json` tracks your progress and is gitignored.
