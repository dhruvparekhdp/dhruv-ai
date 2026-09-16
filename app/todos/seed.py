"""The starting backlog.

Seeded once, on an empty todos table, so the first `todo_next` answers with
real work instead of an empty list. An empty tool gets closed and forgotten;
one that already knows what you owe yourself gets used.

Everything here came out of a planning session, which is why the wording is
specific ("rename tennis-bet", not "tidy up repos"). A vague todo is one you
have to re-decide every time you read it, and re-deciding is the cost this
whole module exists to remove.

Two tracks start active and the rest start parked — see MAX_ACTIVE_TRACKS.
`job_switch` because it is the only item on the list that changes the money,
and `jarvis` because it is what makes the job switch land at a higher band.
"""

from __future__ import annotations

from app.todos.models import Source, TodoStatus, Track
from app.todos import store

#: (title, track, priority, status, notes)
BACKLOG: list[tuple[str, Track, int, TodoStatus, str]] = [
    # -- The visibility fix. Highest value on the list, lowest effort. -----
    (
        "Make dhruv-ai public",
        Track.JARVIS,
        1,
        TodoStatus.NEXT,
        "Multi-agent orchestrator, durable execution, node fleet, 150 tests — "
        "currently invisible. Check `git log --all --full-history -- .env` first; "
        "empty output means it was never committed and it is safe as-is.",
    ),
    (
        "Make ocr public",
        Track.DOCS_TOOL,
        1,
        TodoStatus.NEXT,
        "Air-gapped document intelligence for financial PDFs. Same secrets check first.",
    ),
    (
        "Rename tennis-bet to crypto-signal-engine",
        Track.CRYPTO,
        1,
        TodoStatus.NEXT,
        "The name cost it a fair reading once already. Update the README and the "
        "main.py docstring too. GitHub redirects the old URL.",
    ),
    (
        "Write the Jarvis README for a public audience",
        Track.JARVIS,
        2,
        TodoStatus.INBOX,
        "Lead with the architecture diagram and the 'why not LangGraph' decision — "
        "that is the part that reads as judgement rather than tutorial-following.",
    ),
    # -- Job switch. The only track that moves the number. -----------------
    (
        "Post the LinkedIn post",
        Track.JOB_SWITCH,
        1,
        TodoStatus.NEXT,
        "Drafted long ago, never posted. Post it after the repos are public so the "
        "links resolve.",
    ),
    (
        "Post the LinkedIn article",
        Track.JOB_SWITCH,
        2,
        TodoStatus.INBOX,
        "Then write the Jarvis one: 'I built a multi-agent orchestrator and "
        "deliberately did not use LangGraph'.",
    ),
    (
        "Rewrite LinkedIn headline and About",
        Track.JOB_SWITCH,
        1,
        TodoStatus.NEXT,
        "Must match the resume and the site. All three currently say different things.",
    ),
    (
        "Finish the Arc.dev profile",
        Track.JOB_SWITCH,
        1,
        TodoStatus.NEXT,
        "Started and abandoned. Remote USD contracts clear the target band on their own.",
    ),
    (
        "Naukri: fill every key-skill slot, set 30-day notice",
        Track.JOB_SWITCH,
        2,
        TodoStatus.NEXT,
        "Angular, TypeScript, Node.js, MEAN, MongoDB, Python, GenAI, AWS, "
        "System Design, Team Lead.",
    ),
    (
        "Re-upload the Naukri resume (daily)",
        Track.JOB_SWITCH,
        2,
        TodoStatus.NEXT,
        "Recruiters filter on 'active in last 1 day'. Re-upload, do not edit. "
        "30 seconds, and it is the single highest-leverage habit on this list.",
    ),
    (
        "Send 10 referral messages",
        Track.JOB_SWITCH,
        1,
        TodoStatus.NEXT,
        "Referrals convert far better than portal applications. Applying instead of "
        "asking is why LinkedIn never worked.",
    ),
    (
        "Send the supplier portal POC details for the resume",
        Track.JOB_SWITCH,
        2,
        TodoStatus.NEXT,
        "SwiftUI or UIKit, what it did, whether anyone saw it. Company IP — it goes "
        "on the resume, never on GitHub.",
    ),
    # -- Hardware. Parked track, captured anyway. --------------------------
    (
        "Check RAM slots on the i5 and i3 (dmidecode)",
        Track.PERSONAL,
        2,
        TodoStatus.NEXT,
        "sudo dmidecode -t 16 && sudo dmidecode -t 17. 'Row Of Chips' means soldered.",
    ),
    (
        "f3probe the 2TB NVMe before trusting it",
        Track.PERSONAL,
        1,
        TodoStatus.NEXT,
        "Cheap drives report 2TB and hold 128GB, then corrupt silently. "
        "Do this before a single byte goes on it.",
    ),
    (
        "SMART check the 10-year Seagate",
        Track.PERSONAL,
        3,
        TodoStatus.INBOX,
        "Non-zero reallocated or pending sectors means scratch use only.",
    ),
    (
        "Cap battery at 60% on the always-on laptop",
        Track.PERSONAL,
        2,
        TodoStatus.NEXT,
        "charge_control_end_threshold. A cell held at 100% for months swells and "
        "warps the chassis.",
    ),
    (
        "Set HandleLidSwitch=ignore on the host",
        Track.PERSONAL,
        2,
        TodoStatus.NEXT,
        "The step that decides whether a 24/7 server is actually 24/7.",
    ),
    (
        "scrcpy the Z Flip 4",
        Track.PERSONAL,
        3,
        TodoStatus.INBOX,
        "Black screen, working touch — scrcpy gives back the whole phone. "
        "Everything else on that device depends on it.",
    ),
    (
        "Sell the Ryzen 3 box",
        Track.PERSONAL,
        3,
        TodoStatus.INBOX,
        "4GB, no video out, 50-70W. Roughly 6-10k, and it stops drawing electricity.",
    ),
    # -- Saloni. Parked, but real. -----------------------------------------
    (
        "Decide: is Saloni a business or a portfolio piece",
        Track.SALONI,
        2,
        TodoStatus.INBOX,
        "Mother's business, white-label ambition. If real orders are moving, it "
        "deserves different treatment than it is getting.",
    ),
    # -- Content. Byproduct, never a track of its own. ----------------------
    (
        "Write: why Jarvis does not use LangGraph",
        Track.CONTENT,
        2,
        TodoStatus.INBOX,
        "One artefact, three payoffs: recruiter attention, B2B credibility, and a "
        "prepared answer for the interview question it invites.",
    ),
]

#: Tracks that start active. See the module docstring.
INITIAL_ACTIVE = (Track.JOB_SWITCH, Track.JARVIS)


def seed_if_empty() -> int:
    """Insert the backlog only when the table is empty. Returns rows written.

    Idempotent by design: this runs on every boot, and a restart must not
    duplicate the backlog or resurrect items already completed.
    """
    if store.list_todos(include_done=True, limit=1):
        return 0

    written = 0
    for title, track, priority, status, notes in BACKLOG:
        store.add(
            title,
            track,
            priority=priority,
            status=status,
            notes=notes,
            source=Source.CLAUDE,
        )
        written += 1

    for track in INITIAL_ACTIVE:
        store.set_track_active(track, True, note="active at seed")

    return written
