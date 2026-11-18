"""Facts an LLM is allowed to say about a computed outcome book.

The model narrates. It does not choose the numbers. `render_analysis` is the
only spelling of those numbers, and `narration_problems` rejects a draft that
omits the sample size, the period, or the small-sample warning, or that
introduces a figure the sheet does not contain.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.analytics.outcomes import SMALL_SAMPLE_BELOW, GroupStats, OutcomeAnalysis, OutcomeReport

WARNING = "Sample is below 30 trades; these figures are not a stable edge."
INCOMPLETE = "Sample is incomplete; more trades exist beyond this page."

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_SHEET = "trading-outcomes"


def render_analysis(analysis: OutcomeAnalysis) -> str:
    """One fact sheet. Empty books still state a sample size of zero."""
    lines = [
        _SHEET,
        f"excluded_open: {analysis.excluded_open}",
        f"excluded_research: {analysis.excluded_research}",
        f"excluded_incomplete: {analysis.excluded_incomplete}",
        f"truncated: {str(analysis.truncated).lower()}",
        f"books: {len(analysis.reports)}",
    ]
    if analysis.truncated:
        lines.append(f"warning_incomplete: {INCOMPLETE}")
    if not analysis.reports:
        lines.extend(_empty_book(analysis.truncated))
        return "\n".join(lines)
    for index, report in enumerate(analysis.reports, start=1):
        lines.append(f"book: {index}")
        lines.extend(_report_lines(report))
    return "\n".join(lines)


def narration_problems(text: str, tool_outputs: list[str]) -> list[str]:
    """Problems in `text` given the outcome sheets the tools actually returned.

    No sheet means this check does not apply. An unavailable sheet forbids
    quoting a sample or a win rate, because those figures were not computed.
    """
    sheets = [output for output in tool_outputs if f"\n{_SHEET}\n" in f"\n{output}\n" or output.startswith(_SHEET + "\n")]
    if not sheets:
        return []
    problems: list[str] = []
    for sheet in sheets:
        problems.extend(_check_one(text, sheet))
    return problems


def _empty_book(truncated: bool) -> list[str]:
    lines = [
        "book: 1",
        "mode: none",
        "currency: none",
        "sample_size: 0",
        "period_start: none",
        "period_end: none",
        "small_sample: true",
        f"warning: {WARNING}",
    ]
    if truncated:
        lines.append(f"note: {INCOMPLETE}")
    lines.append("win_rate: undefined")
    return lines


def _report_lines(report: OutcomeReport) -> list[str]:
    lines = [
        f"mode: {report.mode}",
        f"currency: {report.currency}",
        f"sample_size: {report.sample_size}",
        f"period_start: {_stamp(report.period_start)}",
        f"period_end: {_stamp(report.period_end)}",
        f"small_sample: {str(report.small_sample).lower()}",
    ]
    if report.small_sample:
        lines.append(f"warning: {WARNING}")
    if report.truncated:
        lines.append(f"note: {INCOMPLETE}")
    lines.extend(
        [
            f"wins: {report.wins}",
            f"losses: {report.losses}",
            f"breakeven: {report.breakeven}",
            f"win_rate: {_num(report.win_rate)}",
            f"average_win: {_num(report.average_win)}",
            f"average_loss: {_num(report.average_loss)}",
            f"expectancy: {_num(report.expectancy)}",
            f"profit_factor: {_num(report.profit_factor)}",
            f"net_pnl: {_num(report.net_pnl)}",
            f"gross_profit: {_num(report.gross_profit)}",
            f"gross_loss: {_num(report.gross_loss)}",
            f"max_drawdown: {_num(report.max_drawdown)}",
            f"longest_win_streak: {report.longest_win_streak}",
            f"longest_loss_streak: {report.longest_loss_streak}",
            f"current_streak: {report.current_streak_kind} {report.current_streak_length}",
        ]
    )
    for group in report.by_strategy:
        lines.extend(_group_lines("strategy", group.key, group))
    for group in report.by_symbol:
        lines.extend(_group_lines("symbol", group.key, group))
    return lines


def _group_lines(kind: str, key: str, group: GroupStats) -> list[str]:
    prefix = f"{kind}.{key}"
    return [
        f"{prefix}.sample_size: {group.sample_size}",
        f"{prefix}.win_rate: {_num(group.win_rate)}",
        f"{prefix}.average_win: {_num(group.average_win)}",
        f"{prefix}.average_loss: {_num(group.average_loss)}",
        f"{prefix}.expectancy: {_num(group.expectancy)}",
        f"{prefix}.profit_factor: {_num(group.profit_factor)}",
        f"{prefix}.net_pnl: {_num(group.net_pnl)}",
        f"{prefix}.max_drawdown: {_num(group.max_drawdown)}",
    ]


def _check_one(text: str, sheet: str) -> list[str]:
    if "status: unavailable" in sheet or _field(sheet, "status") == "unavailable":
        if re.search(r"sample size\s*:", text, re.IGNORECASE):
            return ["outcome figures were not computed for an unavailable trades read"]
        if re.search(r"win rate\s*:", text, re.IGNORECASE):
            return ["outcome figures were not computed for an unavailable trades read"]
        return []

    blocks = _books(sheet)
    if not blocks:
        return ["outcome sheet has no book"]

    problems: list[str] = []
    allowed = _allowed_numbers(sheet)
    allowed.add(Decimal(SMALL_SAMPLE_BELOW))
    for number in _numbers(_strip_timestamps(text)):
        if number not in allowed:
            problems.append(f"narration cites {_num(number)}, which was not computed")

    any_small = any(block["small_sample"] == "true" for block in blocks)
    if any_small and WARNING not in text:
        problems.append("narration must show the small-sample warning")
    if not any_small and WARNING in text:
        problems.append("narration warns about sample size when the book has 30 or more trades")
    incomplete = "warning_incomplete:" in sheet or f"note: {INCOMPLETE}" in sheet
    if incomplete and INCOMPLETE not in text:
        problems.append("narration must say the trade sample is incomplete")

    for block in blocks:
        sample = block["sample_size"]
        start = block["period_start"]
        end = block["period_end"]
        if f"Sample size: {sample}" not in text:
            problems.append(f"narration must show Sample size: {sample}")
        if start == "none":
            period = "Period: none"
        else:
            period = f"Period: {start} to {end}"
        if period not in text:
            problems.append(f"narration must show {period}")
    return problems


def _books(sheet: str) -> list[dict[str, str]]:
    """Each book is the fields after a `book:` line, up to the next book."""
    books: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in sheet.splitlines():
        if line.startswith("book:"):
            if current is not None:
                books.append(current)
            current = {"truncated": "true" if "warning_incomplete:" in sheet or f"note: {INCOMPLETE}" in sheet else "false"}
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if "." in key:
            continue
        current[key] = value.strip()
    if current is not None:
        books.append(current)
    return [book for book in books if "sample_size" in book]


def _allowed_numbers(sheet: str) -> set[Decimal]:
    found: set[Decimal] = set()
    body = _strip_timestamps(sheet)
    for match in _NUMBER.findall(body):
        try:
            found.add(Decimal(match))
        except InvalidOperation:
            continue
    return found


def _numbers(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    for match in _NUMBER.findall(text):
        try:
            found.append(Decimal(match))
        except InvalidOperation:
            continue
    return found


def _strip_timestamps(text: str) -> str:
    return _TIMESTAMP.sub(" ", text)


def _field(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "none"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Decimal | None) -> str:
    if value is None:
        return "undefined"
    return format(value, "f")
