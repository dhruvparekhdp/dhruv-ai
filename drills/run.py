#!/usr/bin/env python3
"""Jarvis debugging drills.

You are handed a symptom, not a cause. Find the bug, fix it, prove it with the
tests. Every drill is a real bug class from this codebase or its domain -- three
of them are bugs that actually shipped here and had to be found the hard way.

    python3 drills/run.py list             what is available, and your progress
    python3 drills/run.py start 01         plant the bug
    python3 drills/run.py check 01         run the tests that should catch it
    python3 drills/run.py hint 01          one more nudge (repeatable)
    python3 drills/run.py solve 01         reveal and explain
    python3 drills/run.py reset 01         put the file back

Rule of the game: read `check` output before `hint`, and `hint` before `solve`.
The struggle is the part that teaches.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(__file__).resolve().parent / "cases"
STATE = Path(__file__).resolve().parent / ".state.json"

# Case modules do `from drills.run import Drill`, so the repo root has to be
# importable whether this is run as a script or as a module.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Drill:
    id: str
    title: str
    area: str
    difficulty: str          # easy | medium | hard
    minutes: int
    file: str                # repo-relative path the bug lives in
    good: str                # the correct code
    bad: str                 # the broken code
    symptom: str             # what you observe
    tests: list[str]         # pytest node ids that should fail while broken
    hints: list[str] = field(default_factory=list)
    explanation: str = ""
    concept: str = ""        # the transferable idea


# --------------------------------------------------------------------------


def load_drills() -> dict[str, Drill]:
    drills: dict[str, Drill] = {}
    for path in sorted(CASES.glob("drill_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        drill = module.DRILL
        drills[drill.id] = drill
    return drills


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def _swap(drill: Drill, frm: str, to: str) -> bool:
    """Replace one exact block in the target file."""
    path = ROOT / drill.file
    text = path.read_text()
    if frm not in text:
        return False
    path.write_text(text.replace(frm, to, 1))
    return True


# --------------------------------------------------------------------------


C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "r": "\033[31m",
     "y": "\033[33m", "c": "\033[36m", "x": "\033[0m"}


def cmd_list(drills: dict[str, Drill], state: dict) -> None:
    print(f"\n{C['b']}Jarvis debugging drills{C['x']}\n")
    header = f"  {'ID':<4} {'STATUS':<10} {'AREA':<14} {'DIFF':<7} {'MIN':<4} TITLE"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for drill in drills.values():
        st = state.get(drill.id, {})
        if st.get("solved"):
            status, colour = "solved", C["g"]
        elif st.get("active"):
            status, colour = "ACTIVE", C["y"]
        else:
            status, colour = "-", C["dim"]
        print(f"  {drill.id:<4} {colour}{status:<10}{C['x']} {drill.area:<14} "
              f"{drill.difficulty:<7} {drill.minutes:<4} {drill.title}")
    done = sum(1 for s in state.values() if s.get("solved"))
    print(f"\n  {done}/{len(drills)} solved\n")


def cmd_start(drill: Drill, state: dict) -> None:
    entry = state.setdefault(drill.id, {})
    if entry.get("active"):
        print(f"{C['y']}Drill {drill.id} is already planted.{C['x']} "
              f"Run 'check {drill.id}' or 'reset {drill.id}'.")
        return
    if not _swap(drill, drill.good, drill.bad):
        print(f"{C['r']}Could not plant the bug.{C['x']} "
              f"{drill.file} does not contain the expected code -- "
              f"is your working tree clean?")
        return

    entry.update({"active": True, "hints_used": 0})
    save_state(state)

    print(f"\n{C['b']}Drill {drill.id} — {drill.title}{C['x']}")
    print(f"{C['dim']}{drill.area} · {drill.difficulty} · ~{drill.minutes} min{C['x']}\n")
    print(f"{C['b']}Symptom{C['x']}")
    for line in drill.symptom.strip().split("\n"):
        print(f"  {line}")
    print(f"\n{C['b']}Where to start{C['x']}")
    print(f"  The bug is somewhere in this repo. Reproduce it first:")
    print(f"    {C['c']}python3 drills/run.py check {drill.id}{C['x']}")
    print(f"\n{C['dim']}  Stuck? 'hint {drill.id}'. Beaten? 'solve {drill.id}'.{C['x']}\n")


def cmd_check(drill: Drill, state: dict) -> None:
    entry = state.get(drill.id, {})
    if not entry.get("active") and not entry.get("solved"):
        print(f"Drill {drill.id} is not planted. Run 'start {drill.id}'.")
        return

    print(f"\n{C['dim']}Running: pytest {' '.join(drill.tests)}{C['x']}\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *drill.tests],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = result.stdout.strip().split("\n")
    print("\n".join(tail[-25:]))

    if result.returncode == 0:
        if entry.get("active"):
            entry.update({"active": False, "solved": True})
            save_state(state)
            print(f"\n{C['g']}{C['b']}Fixed.{C['x']} "
                  f"Now read why it mattered: {C['c']}solve {drill.id}{C['x']}\n")
    else:
        print(f"\n{C['r']}Still failing.{C['x']} "
              f"Read the assertion above -- it tells you what was expected "
              f"versus what happened.\n")


def cmd_hint(drill: Drill, state: dict) -> None:
    entry = state.setdefault(drill.id, {})
    used = entry.get("hints_used", 0)
    if used >= len(drill.hints):
        print(f"{C['y']}No hints left.{C['x']} Try 'solve {drill.id}'.")
        return
    print(f"\n{C['y']}Hint {used + 1}/{len(drill.hints)}{C['x']}")
    print(f"  {drill.hints[used]}\n")
    entry["hints_used"] = used + 1
    save_state(state)


def cmd_solve(drill: Drill, state: dict) -> None:
    print(f"\n{C['b']}Drill {drill.id} — {drill.title}{C['x']}\n")
    print(f"{C['b']}The bug{C['x']}")
    print(f"  {drill.file}\n")
    print(f"{C['r']}  broken:{C['x']}")
    for line in drill.bad.strip().split("\n"):
        print(f"    {line}")
    print(f"\n{C['g']}  correct:{C['x']}")
    for line in drill.good.strip().split("\n"):
        print(f"    {line}")
    print(f"\n{C['b']}Why{C['x']}")
    for line in drill.explanation.strip().split("\n"):
        print(f"  {line}")
    print(f"\n{C['b']}The transferable idea{C['x']}")
    print(f"  {C['c']}{drill.concept}{C['x']}\n")

    entry = state.setdefault(drill.id, {})
    entry["revealed"] = True
    save_state(state)


def cmd_reset(drill: Drill, state: dict) -> None:
    if _swap(drill, drill.bad, drill.good):
        print(f"{C['g']}Restored {drill.file}.{C['x']}")
    else:
        print(f"{C['dim']}Nothing to restore -- {drill.file} is already correct.{C['x']}")
    entry = state.setdefault(drill.id, {})
    entry["active"] = False
    save_state(state)


def cmd_reset_all(drills: dict[str, Drill], state: dict) -> None:
    for drill in drills.values():
        _swap(drill, drill.bad, drill.good)
    for entry in state.values():
        entry["active"] = False
    save_state(state)
    print(f"{C['g']}All drills reset.{C['x']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis debugging drills")
    parser.add_argument("command",
                        choices=["list", "start", "check", "hint", "solve", "reset", "reset-all"])
    parser.add_argument("drill_id", nargs="?")
    args = parser.parse_args()

    drills = load_drills()
    state = load_state()

    if args.command == "list":
        cmd_list(drills, state)
        return
    if args.command == "reset-all":
        cmd_reset_all(drills, state)
        return

    if not args.drill_id:
        print("Which drill? e.g.  python3 drills/run.py start 01")
        return
    drill = drills.get(args.drill_id.zfill(2))
    if drill is None:
        print(f"No drill '{args.drill_id}'. Run 'list' to see them.")
        return

    {"start": cmd_start, "check": cmd_check, "hint": cmd_hint,
     "solve": cmd_solve, "reset": cmd_reset}[args.command](drill, state)


if __name__ == "__main__":
    main()
