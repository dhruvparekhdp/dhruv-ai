"""Intent classification tests.

The regression that matters most: free-text mood notes must stay in
PERSONAL_CHAT. The design doc's substring matcher routed "had a great run
today" to OS_AUTOMATION, which would make the mood feature nonsense.
"""

from __future__ import annotations

import pytest

from app.agent.router import (
    CODING_AGENT,
    OS_AUTOMATION,
    PERSONAL_CHAT,
    QUANT_TRADING,
    classify_intent,
    is_supported,
)


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        # Greetings and mood -- the entire Phase 1 surface.
        ("hi", PERSONAL_CHAT),
        ("hello there", PERSONAL_CHAT),
        ("Mood check-in: 4/5 (good).", PERSONAL_CHAT),
        ("feeling pretty low today, didn't sleep", PERSONAL_CHAT),
        # Substrings that must NOT trigger a subsystem.
        ("had a great run today and felt good", PERSONAL_CHAT),
        ("my sister is buying a house and I'm happy for her", PERSONAL_CHAT),
        ("work was a grind but I got through it", PERSONAL_CHAT),
        # Real subsystem requests.
        ("run the command ls -la in my home dir", OS_AUTOMATION),
        ("what's my CPU usage right now", OS_AUTOMATION),
        ("refactor the auth module and run pytest", CODING_AGENT),
        ("commit this to git and open a pull request", CODING_AGENT),
        ("what's my portfolio P&L today", QUANT_TRADING),
        ("sell my bitcoin position", QUANT_TRADING),
    ],
)
def test_classify_intent(prompt: str, expected: str) -> None:
    assert classify_intent(prompt) == expected


def test_empty_prompt_defaults_to_chat() -> None:
    assert classify_intent("") == PERSONAL_CHAT
    assert classify_intent("   ") == PERSONAL_CHAT


def test_coding_wins_over_trading_when_both_match() -> None:
    """Ordering guard: 'sell' inside a coding request must not route to trading."""
    assert classify_intent("refactor the code that sells positions") == CODING_AGENT


def test_only_chat_is_supported_in_phase_one() -> None:
    assert is_supported(PERSONAL_CHAT)
    assert not is_supported(OS_AUTOMATION)
    assert not is_supported(CODING_AGENT)
    assert not is_supported(QUANT_TRADING)
