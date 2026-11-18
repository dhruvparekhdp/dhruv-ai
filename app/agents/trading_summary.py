"""Validate an analyst summary against the trading reads that produced it.

The model writes the summary. This module decides whether that text is allowed
to stand. A symbol that was not in a positions read is not a holding, a stale
or dead feed has to be named before any conclusion, and the source engine plus
`data_as_of` have to be copied from the tool header rather than invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTRACT_INSTRUCTIONS = """
Trading summary contract (required):
- Call a trading read tool before answering. Copy source_engine and data_as_of from that tool's header. Do not invent either.
- If any read is unavailable or invalid, the first line must start with UNAVAILABLE: and name each such source_engine with "data_as_of unknown". Say that before any conclusion.
- Otherwise, if any read is stale, the first line must start with STALE: and name each stale source_engine with its data_as_of. Say that before any conclusion.
- Then emit SOURCE: and DATA_AS_OF: lines that match every read.
- Emit exactly one HOLDINGS: line. Use "none", or comma-separated symbols copied from a positions read. A signal symbol is not a holding. Never name a symbol the positions read did not return.
- End with a non-empty "## Assumptions" section and a non-empty "## Uncertainty" section.
- You cannot place, cancel, amend, withdraw, or transfer. No such tool exists.
- If an outcomes tool was called, narrate its sheet. Include every "Sample size:" and "Period:" line. If the sheet warns that the sample is below 30 trades, copy that warning. Do not invent or recompute a win rate, expectancy, drawdown, or streak.
""".strip()

_HEADER = "trading-v1"
_HOLDINGS_LINE = re.compile(r"^HOLDINGS:\s*(.*)$", re.MULTILINE)
_SOURCE_LINE = re.compile(r"^SOURCE:\s*(\S+)\s*$", re.MULTILINE)
_AS_OF_LINE = re.compile(r"^DATA_AS_OF:\s*(\S+)\s*$", re.MULTILINE)
_SECTION = re.compile(r"^## ([A-Za-z][A-Za-z ]*)\s*$", re.MULTILINE)
_HOLDING_WORD = re.compile(r"\b(?:holding|holds|held)\s+([A-Z][A-Z0-9]{1,19})\b")


@dataclass(frozen=True)
class FeedEvidence:
    """One trading read, reduced to the fields a summary is allowed to cite."""

    outcome: str
    stale: bool
    unavailable: bool
    source_engine: str
    data_as_of: str
    resource: str
    holdings: frozenset[str]


def evidence_from_output(output: str) -> FeedEvidence | None:
    """Parse a trading tool header. None when the text is not one."""
    if not output.startswith(_HEADER + "\n"):
        return None
    header = output.split("\n---\n", 1)[0]
    fields: dict[str, str] = {}
    for line in header.splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    outcome = fields.get("outcome", "")
    data_as_of = fields.get("data_as_of") or "unknown"
    unavailable = outcome in {"unavailable", "invalid"} or data_as_of == "unknown"
    holdings = frozenset(part for part in fields.get("holdings", "").split(",") if part)
    return FeedEvidence(
        outcome=outcome,
        stale=fields.get("stale") == "true" and not unavailable,
        unavailable=unavailable,
        source_engine=fields.get("source_engine", ""),
        data_as_of=data_as_of,
        resource=fields.get("resource", ""),
        holdings=holdings,
    )


def evidence_from_outputs(outputs: list[str]) -> list[FeedEvidence]:
    found: list[FeedEvidence] = []
    for output in outputs:
        item = evidence_from_output(output)
        if item is not None:
            found.append(item)
    return found


def validate_trading_summary(text: str, evidence: list[FeedEvidence]) -> list[str]:
    """Return problems. An empty list means the summary may be stored."""
    if not evidence:
        return ["no trading read is in the evidence"]

    problems: list[str] = []
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    unavailable = [item for item in evidence if item.unavailable]
    stale = [item for item in evidence if item.stale]

    if unavailable:
        if not first.startswith("UNAVAILABLE:"):
            problems.append("unavailable input must be stated on the first line")
        else:
            for item in unavailable:
                if item.source_engine not in first or item.data_as_of not in first:
                    problems.append(
                        f"first line must name {item.source_engine} data_as_of {item.data_as_of}"
                    )
    elif stale:
        if not first.startswith("STALE:"):
            problems.append("stale input must be stated on the first line")
        else:
            for item in stale:
                if item.source_engine not in first or item.data_as_of not in first:
                    problems.append(
                        f"first line must name {item.source_engine} data_as_of {item.data_as_of}"
                    )
    elif first.startswith("STALE:") or first.startswith("UNAVAILABLE:"):
        problems.append("summary claims stale or unavailable data when every read is fresh")

    sources = _SOURCE_LINE.findall(text)
    stamps = _AS_OF_LINE.findall(text)
    if len(sources) != len(stamps):
        problems.append("SOURCE and DATA_AS_OF lines must pair")
    else:
        expected = {(item.source_engine, item.data_as_of) for item in evidence}
        stated = set(zip(sources, stamps))
        if stated != expected:
            problems.append("SOURCE and DATA_AS_OF must match the adapter reads")

    holding_lines = _HOLDINGS_LINE.findall(text)
    if len(holding_lines) != 1:
        problems.append("exactly one HOLDINGS line is required")
        claimed: set[str] = set()
    else:
        raw = holding_lines[0].strip()
        if raw == "none":
            claimed = set()
        elif not raw or "none" in {part.strip() for part in raw.split(",")}:
            problems.append("HOLDINGS must be 'none' or a list of symbols")
            claimed = set()
        else:
            claimed = {part.strip() for part in raw.split(",") if part.strip()}

    # Only positions reads contribute symbols. Signals are not holdings.
    known = set().union(*(item.holdings for item in evidence if item.resource == "positions"))
    invented = sorted(symbol for symbol in claimed if symbol not in known)
    if invented:
        problems.append(
            "invented holding rejected: " + ", ".join(invented) + " is not in the adapter positions"
        )

    for symbol in sorted(set(_HOLDING_WORD.findall(text)) - known):
        problems.append(
            "invented holding rejected: " + symbol + " is not in the adapter positions"
        )

    for title in ("Assumptions", "Uncertainty"):
        if not _section_body(text, title):
            problems.append(f"{title} section is required")

    return problems


def retry_message(problems: list[str]) -> str:
    lines = "\n".join(f"- {problem}" for problem in problems)
    return (
        "The summary was rejected. Rewrite it from the tool headers. "
        "Do not present a rejected symbol as a holding.\n"
        f"{lines}"
    )


def rejection_text(problems: list[str]) -> str:
    """What is stored when retries are exhausted. Not a holdings list."""
    lines = ["[summary rejected]", "The draft was not stored."]
    for problem in problems:
        lines.append(f"- {problem}")
    return "\n".join(lines)


def _section_body(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: nxt.start()] if nxt else rest
    return body.strip()
