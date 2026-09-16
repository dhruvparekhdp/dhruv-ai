"""Intent classification -- the first node of the orchestrator graph.

Phase 1 uses fast deterministic keyword matching rather than an LLM call: it
costs nothing, adds no latency, and the four categories are far apart. Later
phases can swap in a model-based classifier behind the same signature.

One deliberate change from the design doc's snippet: matching is on **word
boundaries**, not raw substrings. The doc's version does `"run" in prompt`,
which misroutes ordinary mood notes -- "had a great run today" would be
classified as a terminal command. With `\\brun\\b` plus phrase anchoring for the
weakest keywords, free-text journalling stays in PERSONAL_CHAT where it belongs.
"""

from __future__ import annotations

import re
from typing import Final

OS_AUTOMATION: Final = "OS_AUTOMATION"
CODING_AGENT: Final = "CODING_AGENT"
QUANT_TRADING: Final = "QUANT_TRADING"
PERSONAL_CHAT: Final = "PERSONAL_CHAT"

INTENTS: Final = (OS_AUTOMATION, CODING_AGENT, QUANT_TRADING, PERSONAL_CHAT)

# Ordered most-specific first: a prompt mentioning both "git" and "sell" is far
# more likely a coding task than a trade.
_PATTERNS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        CODING_AGENT,
        (
            r"\bcode\b", r"\bcodebase\b", r"\brefactor\w*\b", r"\bdebug\w*\b",
            r"\bpytest\b", r"\bunit test\w*\b", r"\bgit\b", r"\bcommit\b",
            r"\bpull request\b", r"\brepo(sitory)?\b", r"\bfunction\b",
            r"\bpython\b", r"\bjavascript\b", r"\btypescript\b", r"\bstack ?trace\b",
            r"\bcompile\w*\b", r"\blint\w*\b",
        ),
    ),
    (
        QUANT_TRADING,
        (
            r"\btrade\b", r"\btrading\b", r"\bportfolio\b", r"\bticker\b",
            r"\bcrypto\b", r"\bbitcoin\b", r"\bethereum\b", r"\bstock\w*\b",
            r"\bequit(y|ies)\b", r"\bmarket\b", r"\bposition\w*\b",
            r"\bbuy\b", r"\bsell\b", r"\border book\b", r"\bp\W?n\W?l\b",
            r"\bdrawdown\b", r"\bkill switch\b",
        ),
    ),
    (
        OS_AUTOMATION,
        (
            r"\bterminal\b", r"\bbash\b", r"\bshell\b", r"\bcommand line\b",
            r"\bcpu\b", r"\bram\b", r"\bmemory usage\b", r"\bdisk\b",
            r"\bprocess(es)?\b", r"\bkill\b", r"\breboot\b", r"\bshutdown\b",
            r"\bsystem (status|health|info)\b", r"\brun (the )?(command|script)\b",
            r"\bls\b", r"\bchmod\b", r"\bsudo\b",
        ),
    ),
)

_COMPILED: Final = tuple(
    (intent, re.compile("|".join(patterns), re.IGNORECASE))
    for intent, patterns in _PATTERNS
)


def classify_intent(prompt: str) -> str:
    """Map a user prompt to one of the four operational categories.

    Defaults to PERSONAL_CHAT, which is both the safe fallback and the entire
    surface area of Phase 1.
    """
    if not prompt or not prompt.strip():
        return PERSONAL_CHAT

    for intent, pattern in _COMPILED:
        if pattern.search(prompt):
            return intent

    return PERSONAL_CHAT


def is_supported(intent: str) -> bool:
    """Whether this build can actually execute the intent.

    Phase 1 ships conversation only. OS control, the coding agent, and the
    trading engine are recognised by the router but not yet wired to tools --
    so Jarvis says so plainly instead of hallucinating that it ran something.
    """
    return intent == PERSONAL_CHAT
