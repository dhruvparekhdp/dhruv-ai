"""Estimated USD prices for LLM calls.

These are estimates so a budget can be a number, not an invoice. Override any
row with JARVIS_PRICE_TABLE, a JSON object keyed by ``PROVIDER/model``:

    {"GROQ/openai/gpt-oss-120b": {"input_per_million": 0.15, "output_per_million": 0.60}}

Rates are USD per one million tokens. The local echo model is explicitly free.
An unknown model uses a fallback rate and is marked estimated, so a cloud call
is never recorded as costing nothing just because the table has no row for it.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation

log = logging.getLogger("jarvis.pricing")

# USD per million tokens: (input, output). Estimates, overridable.
_DEFAULTS: dict[str, tuple[Decimal, Decimal]] = {
    "GROQ/openai/gpt-oss-120b": (Decimal("0.15"), Decimal("0.60")),
    "GEMINI/gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "ECHO/jarvis-local-echo": (Decimal("0"), Decimal("0")),
}

# Used when the table has no row. High enough that an unknown cloud model
# cannot spend for free. price_known is False for this path.
_FALLBACK = (Decimal("1"), Decimal("1"))


def rates() -> dict[str, tuple[Decimal, Decimal]]:
    """Defaults overlaid with JARVIS_PRICE_TABLE. A bad table is ignored."""
    table = dict(_DEFAULTS)
    raw = os.getenv("JARVIS_PRICE_TABLE", "").strip()
    if not raw:
        return table
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("JARVIS_PRICE_TABLE is not valid JSON; using the built-in prices")
        return table
    if not isinstance(parsed, dict):
        log.warning("JARVIS_PRICE_TABLE must be a JSON object; using the built-in prices")
        return table
    for key, value in parsed.items():
        pair = _pair(value)
        if pair is None:
            log.warning("ignoring price row %s; expected input_per_million and output_per_million", key)
            continue
        table[str(key)] = pair
    return table


def estimate(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> tuple[int, bool]:
    """Return (cost in micro-USD, whether the table had a row for this model).

    One micro-USD is one millionth of a dollar. Integer money keeps SQLite and
    Postgres on the same number.
    """
    table = rates()
    key = f"{provider}/{model}"
    wildcard = f"{provider}/*"
    if key in table:
        input_rate, output_rate = table[key]
        known = True
    elif wildcard in table:
        input_rate, output_rate = table[wildcard]
        known = True
    else:
        input_rate, output_rate = _FALLBACK
        known = False

    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    # micros = tokens * (USD per million tokens). See the module docstring.
    usd = (Decimal(prompt) * input_rate + Decimal(completion) * output_rate) / Decimal(1_000_000)
    micros = int((usd * Decimal(1_000_000)).to_integral_value())
    return micros, known


def _pair(value: object) -> tuple[Decimal, Decimal] | None:
    if not isinstance(value, dict):
        return None
    try:
        return (
            Decimal(str(value["input_per_million"])),
            Decimal(str(value["output_per_million"])),
        )
    except (KeyError, InvalidOperation, ValueError):
        return None
