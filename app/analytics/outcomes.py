"""Paper and shadow outcome stats computed from closed trades.

The arithmetic is Decimal and does not call a model. Definitions, so a test
can be hand-checked:

- A trade counts only when it is paper or shadow, has both `closed_at` and
  `pnl`, and the pnl currency is the book. Research is not an outcome.
  Open or half-filled rows are excluded. Fees are not applied again; `pnl`
  is the outcome the engine already reported.
- Books are split by `(mode, currency)`. A shadow USDT result never moves
  a paper USDT drawdown.
- Sample size is the count of included trades. Win rate is wins / sample
  size. A win is `pnl > 0`, a loss is `pnl < 0`, and zero is a breakeven
  that stays in the denominator.
- Average win is the mean of winning pnls. Average loss is the mean of
  the absolute losing pnls. Either is undefined when that side has no trades.
- Expectancy is the mean pnl, which is
  `(win_rate * average_win) - (loss_rate * average_loss)` when breakevens
  contribute zero.
- Profit factor is gross profit / gross loss. Undefined when there is no
  loss (including an empty book). Zero when there were losses and no profit.
- Max drawdown walks the equity of cumulative pnl in `(closed_at, id)` order,
  starting at zero. At each trade the drawdown is `peak - equity` when that
  is positive. The max of those values is the result, in pnl currency, not
  in percent.
- Streaks use that same order. A breakeven ends the run. The current streak
  is the trailing run.
- Fewer than 30 included trades is a small sample. The figure is still
  returned; callers must show the warning rather than drop the book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from pydantic import ValidationError

from app.contracts.trading_v1.models import Trade

SMALL_SAMPLE_BELOW = 30
INCLUDED_MODES = frozenset({"paper", "shadow"})


@dataclass(frozen=True)
class GroupStats:
    key: str
    sample_size: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class OutcomeReport:
    mode: str
    currency: str
    sample_size: int
    period_start: datetime | None
    period_end: datetime | None
    small_sample: bool
    truncated: bool
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    max_drawdown: Decimal
    # Peak-to-trough as a fraction of the peak. None when the book never
    # trades above zero, so a percent alert has nothing honest to compare.
    max_drawdown_pct: Decimal | None
    longest_win_streak: int
    longest_loss_streak: int
    current_streak_kind: str
    current_streak_length: int
    by_strategy: tuple[GroupStats, ...]
    by_symbol: tuple[GroupStats, ...]


@dataclass(frozen=True)
class OutcomeAnalysis:
    reports: tuple[OutcomeReport, ...]
    excluded_open: int
    excluded_research: int
    excluded_incomplete: int
    truncated: bool


@dataclass(frozen=True)
class _Row:
    id: str
    symbol: str
    strategy: str
    mode: str
    currency: str
    pnl: Decimal
    closed_at: datetime


def compute_outcomes(trades: Iterable[Any], *, truncated: bool = False) -> OutcomeAnalysis:
    """Split `trades` into paper and shadow books and score each one.

    `truncated` is true when the caller knows the list is not the full
    history (a page with a next cursor). It does not change the arithmetic.
    """
    included: list[_Row] = []
    excluded_open = 0
    excluded_research = 0
    excluded_incomplete = 0

    for raw in trades:
        trade = _coerce(raw)
        if trade.mode.value == "research":
            excluded_research += 1
            continue
        if trade.mode.value not in INCLUDED_MODES:
            # Trade.mode has no live value. Anything else is not a book.
            excluded_incomplete += 1
            continue
        if trade.closed_at is None and trade.pnl is None:
            excluded_open += 1
            continue
        if trade.closed_at is None or trade.pnl is None:
            excluded_incomplete += 1
            continue
        included.append(
            _Row(
                id=trade.id,
                symbol=trade.symbol,
                strategy=trade.strategy,
                mode=trade.mode.value,
                currency=trade.pnl.currency,
                pnl=Decimal(trade.pnl.amount),
                closed_at=trade.closed_at,
            )
        )

    included.sort(key=lambda row: (row.closed_at, row.id))
    books: dict[tuple[str, str], list[_Row]] = {}
    for row in included:
        books.setdefault((row.mode, row.currency), []).append(row)

    reports = tuple(
        _report(mode, currency, rows, truncated=truncated)
        for (mode, currency), rows in sorted(books.items())
    )
    return OutcomeAnalysis(
        reports=reports,
        excluded_open=excluded_open,
        excluded_research=excluded_research,
        excluded_incomplete=excluded_incomplete,
        truncated=truncated,
    )


def _coerce(raw: Any) -> Trade:
    if isinstance(raw, Trade):
        return raw
    try:
        return Trade.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("trade did not match trading-v1") from exc


def _report(mode: str, currency: str, rows: list[_Row], *, truncated: bool) -> OutcomeReport:
    stats = _metrics(rows)
    kind, length, longest_win, longest_loss = _streaks(rows)
    return OutcomeReport(
        mode=mode,
        currency=currency,
        sample_size=stats.sample_size,
        period_start=rows[0].closed_at,
        period_end=rows[-1].closed_at,
        small_sample=stats.sample_size < SMALL_SAMPLE_BELOW,
        truncated=truncated,
        wins=stats.wins,
        losses=stats.losses,
        breakeven=stats.breakeven,
        win_rate=stats.win_rate,
        average_win=stats.average_win,
        average_loss=stats.average_loss,
        expectancy=stats.expectancy,
        profit_factor=stats.profit_factor,
        net_pnl=stats.net_pnl,
        gross_profit=stats.gross_profit,
        gross_loss=stats.gross_loss,
        max_drawdown=stats.max_drawdown,
        max_drawdown_pct=_max_drawdown_pct(rows),
        longest_win_streak=longest_win,
        longest_loss_streak=longest_loss,
        current_streak_kind=kind,
        current_streak_length=length,
        by_strategy=_groups(rows, "strategy"),
        by_symbol=_groups(rows, "symbol"),
    )


def _groups(rows: list[_Row], field: str) -> tuple[GroupStats, ...]:
    buckets: dict[str, list[_Row]] = {}
    for row in rows:
        buckets.setdefault(getattr(row, field), []).append(row)
    return tuple(_metrics(buckets[key], key=key) for key in sorted(buckets))


def _metrics(rows: list[_Row], *, key: str = "") -> GroupStats:
    wins = [row.pnl for row in rows if row.pnl > 0]
    losses = [row.pnl for row in rows if row.pnl < 0]
    breakeven = sum(1 for row in rows if row.pnl == 0)
    gross_profit = sum(wins, Decimal(0))
    gross_loss = sum((-pnl for pnl in losses), Decimal(0))
    net = gross_profit - gross_loss
    sample = len(rows)
    return GroupStats(
        key=key,
        sample_size=sample,
        wins=len(wins),
        losses=len(losses),
        breakeven=breakeven,
        win_rate=_ratio(len(wins), sample),
        average_win=_ratio(gross_profit, len(wins)),
        average_loss=_ratio(gross_loss, len(losses)),
        expectancy=_ratio(net, sample),
        profit_factor=_profit_factor(gross_profit, gross_loss),
        net_pnl=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        max_drawdown=_max_drawdown(rows),
    )


def _ratio(numerator: Decimal | int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _profit_factor(gross_profit: Decimal, gross_loss: Decimal) -> Decimal | None:
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def _max_drawdown(rows: list[_Row]) -> Decimal:
    peak = Decimal(0)
    equity = Decimal(0)
    worst = Decimal(0)
    for row in rows:
        equity += row.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > worst:
            worst = drawdown
    return worst


def _max_drawdown_pct(rows: list[_Row]) -> Decimal | None:
    """Same walk as `_max_drawdown`, as `(peak - equity) / peak`.

    Currency drawdown cannot answer "drawdown > Y%". A peak that never rises
    above zero has no denominator; callers must not invent 100%.
    """
    peak = Decimal(0)
    equity = Decimal(0)
    worst: Decimal | None = None
    for row in rows:
        equity += row.pnl
        if equity > peak:
            peak = equity
        if peak <= 0:
            continue
        pct = (peak - equity) / peak
        if worst is None or pct > worst:
            worst = pct
    return worst


def _streaks(rows: list[_Row]) -> tuple[str, int, int, int]:
    longest_win = 0
    longest_loss = 0
    kind = "none"
    length = 0
    for row in rows:
        if row.pnl > 0:
            this = "win"
        elif row.pnl < 0:
            this = "loss"
        else:
            kind = "none"
            length = 0
            continue
        if this == kind:
            length += 1
        else:
            kind = this
            length = 1
        if kind == "win":
            longest_win = max(longest_win, length)
        else:
            longest_loss = max(longest_loss, length)
    return kind, length, longest_win, longest_loss
