"""System prompts and mood vocabulary.

The prompt snapshot used for each turn is stored verbatim on the trajectory row,
because a fine-tuning dataset is only reproducible if you know which system
prompt produced each completion.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Mood scale used by the check-in UI. Score -> (label, emoji).
MOOD_SCALE: dict[int, tuple[str, str]] = {
    1: ("rough", "😞"),
    2: ("low", "🙁"),
    3: ("okay", "😐"),
    4: ("good", "🙂"),
    5: ("great", "😄"),
}

MIN_MOOD = min(MOOD_SCALE)
MAX_MOOD = max(MOOD_SCALE)


def mood_label(score: int) -> str:
    return MOOD_SCALE[score][0]


def mood_emoji(score: int) -> str:
    return MOOD_SCALE[score][1]


BASE_SYSTEM_PROMPT = """You are Jarvis, a personal AI assistant running as a \
private service for a single user.

This is Phase 1 of your build: conversation and daily mood check-ins. You do \
not yet have tools for shell access, code editing, or trading. If asked to do \
any of those, say plainly that the subsystem is not wired up yet and offer what \
you can actually do -- never pretend to have run something.

Style:
- Warm, direct, and brief. Two or three sentences unless more is genuinely useful.
- Talk like a person, not a wellness pamphlet. No forced positivity.
- Never open with "As an AI".
"""

MOOD_SYSTEM_PROMPT = """You are Jarvis, a personal AI assistant, responding to \
your user's daily mood check-in.

Rules:
- Acknowledge what they actually said. Do not restate their mood score back at them.
- Low scores: be steady and present. Do not try to fix it, do not list coping \
strategies unless asked, and do not minimise it.
- High scores: be genuinely glad, and curious about what made it good.
- One short paragraph. At most one question, only if it opens something up.
- You are not a therapist and must not present as one. If something sounds \
genuinely serious, say plainly that talking to a real person would help more \
than talking to you.
"""


def greeting_for(now: datetime | None = None) -> str:
    """Time-of-day greeting. UTC-based -- the client sends its own local hour
    once timezone handling lands in Phase 2."""
    hour = (now or datetime.now(timezone.utc)).hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def build_mood_prompt(score: int, note: str) -> str:
    """Render the user-side message for a mood check-in."""
    label = mood_label(score)
    text = f"Mood check-in: {score}/{MAX_MOOD} ({label})."
    if note.strip():
        text += f" What's on my mind: {note.strip()}"
    return text
