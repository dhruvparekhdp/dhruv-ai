"""Hand-computed paper and shadow outcome stats.

Expected values are written as literals. They are not produced by calling the
function under test a second way.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.adapters.models import Outcome
from app.adapters.trading.base import TradingRead
from app.agents.runner import AgentRunner
from app.agents.trading_summary import evidence_from_output
from app.analytics.narration import INCOMPLETE, WARNING, narration_problems, render_analysis
from app.analytics.outcomes import compute_outcomes
from app.contracts.trading_v1.models import TradeResponse
from app.tools.cloud.trading import set_trading_adapter, trading_crypto_outcomes
from tests.conftest import scripted_router, text_result, tool_result

FIXTURES = Path("docs/contracts/trading-v1/fixtures")

T1 = "2026-11-01T00:00:00Z"
T2 = "2026-11-02T00:00:00Z"
T3 = "2026-11-03T00:00:00Z"
T4 = "2026-11-04T00:00:00Z"
T5 = "2026-11-05T00:00:00Z"
T6 = "2026-11-06T00:00:00Z"


def _trade(
    trade_id: str,
    symbol: str,
    strategy: str,
    pnl: str | None,
    closed_at: str | None,
    *,
    mode: str = "paper",
    currency: str = "USDT",
) -> dict:
    return {
        "id": trade_id,
        "symbol": symbol,
        "side": "buy",
        "qty": "1",
        "price": {"amount": "1", "currency": currency},
        "fees": {"amount": "0", "currency": currency},
        "opened_at": closed_at or T1,
        "closed_at": closed_at,
        "pnl": None if pnl is None else {"amount": pnl, "currency": currency},
        "strategy": strategy,
        "mode": mode,
    }


def _book() -> list[dict]:
    """Six closed paper trades.

    Close order and pnl: +10, +5, -3, -3, -3, +6.
    Wins 10+5+6 = 21. Losses 3+3+3 = 9. Net 12. Sample 6.
    Equity from 0: 10, 15, 12, 9, 6, 12. Peak 15. Max drawdown 9.
    Streaks: win, win, loss, loss, loss, win. Longest win 2, longest loss 3,
    current win 1.
    """
    return [
        _trade("t1", "BTCUSDT", "momentum", "10", T1),
        _trade("t2", "BTCUSDT", "momentum", "5", T2),
        _trade("t3", "ETHUSDT", "momentum", "-3", T3),
        _trade("t4", "ETHUSDT", "breakout", "-3", T4),
        _trade("t5", "SOLUSDT", "breakout", "-3", T5),
        _trade("t6", "SOLUSDT", "breakout", "6", T6),
    ]


def _paper(trades: list[dict]):
    analysis = compute_outcomes(trades)
    assert len(analysis.reports) == 1
    return analysis, analysis.reports[0]


def test_hand_book_matches_every_headline_figure() -> None:
    analysis, report = _paper(_book())

    assert analysis.excluded_open == 0
    assert analysis.excluded_research == 0
    assert analysis.excluded_incomplete == 0
    assert analysis.truncated is False

    assert report.mode == "paper"
    assert report.currency == "USDT"
    assert report.sample_size == 6
    assert report.wins == 3
    assert report.losses == 3
    assert report.breakeven == 0
    assert report.win_rate == Decimal(1) / Decimal(2)
    assert report.average_win == Decimal(7)
    assert report.average_loss == Decimal(3)
    assert report.expectancy == Decimal(2)
    assert report.profit_factor == Decimal(21) / Decimal(9)
    assert report.profit_factor == Decimal(7) / Decimal(3)
    assert report.net_pnl == Decimal(12)
    assert report.gross_profit == Decimal(21)
    assert report.gross_loss == Decimal(9)
    assert report.max_drawdown == Decimal(9)
    assert report.longest_win_streak == 2
    assert report.longest_loss_streak == 3
    assert report.current_streak_kind == "win"
    assert report.current_streak_length == 1
    assert report.small_sample is True
    assert report.period_start.isoformat() == "2026-11-01T00:00:00+00:00"
    assert report.period_end.isoformat() == "2026-11-06T00:00:00+00:00"


def test_hand_book_by_strategy_and_symbol() -> None:
    _analysis, report = _paper(_book())
    momentum = next(group for group in report.by_strategy if group.key == "momentum")
    breakout = next(group for group in report.by_strategy if group.key == "breakout")
    btc = next(group for group in report.by_symbol if group.key == "BTCUSDT")
    eth = next(group for group in report.by_symbol if group.key == "ETHUSDT")
    sol = next(group for group in report.by_symbol if group.key == "SOLUSDT")

    assert [group.key for group in report.by_strategy] == ["breakout", "momentum"]
    assert [group.key for group in report.by_symbol] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    assert momentum.sample_size == 3
    assert momentum.wins == 2 and momentum.losses == 1
    assert momentum.win_rate == Decimal(2) / Decimal(3)
    assert momentum.average_win == Decimal("7.5")
    assert momentum.average_loss == Decimal(3)
    assert momentum.expectancy == Decimal(4)
    assert momentum.profit_factor == Decimal(5)
    assert momentum.net_pnl == Decimal(12)
    assert momentum.max_drawdown == Decimal(3)

    assert breakout.sample_size == 3
    assert breakout.wins == 1 and breakout.losses == 2
    assert breakout.win_rate == Decimal(1) / Decimal(3)
    assert breakout.average_win == Decimal(6)
    assert breakout.average_loss == Decimal(3)
    assert breakout.expectancy == Decimal(0)
    assert breakout.profit_factor == Decimal(1)
    assert breakout.net_pnl == Decimal(0)
    assert breakout.max_drawdown == Decimal(6)

    assert btc.sample_size == 2 and btc.wins == 2 and btc.losses == 0
    assert btc.win_rate == Decimal(1)
    assert btc.average_win == Decimal("7.5")
    assert btc.average_loss is None
    assert btc.expectancy == Decimal("7.5")
    assert btc.profit_factor is None
    assert btc.net_pnl == Decimal(15)
    assert btc.max_drawdown == Decimal(0)

    assert eth.sample_size == 2 and eth.wins == 0 and eth.losses == 2
    assert eth.win_rate == Decimal(0)
    assert eth.average_win is None
    assert eth.average_loss == Decimal(3)
    assert eth.expectancy == Decimal(-3)
    assert eth.profit_factor == Decimal(0)
    assert eth.net_pnl == Decimal(-6)
    assert eth.max_drawdown == Decimal(6)

    assert sol.sample_size == 2 and sol.wins == 1 and sol.losses == 1
    assert sol.win_rate == Decimal(1) / Decimal(2)
    assert sol.average_win == Decimal(6)
    assert sol.average_loss == Decimal(3)
    assert sol.expectancy == Decimal("1.5")
    assert sol.profit_factor == Decimal(2)
    assert sol.net_pnl == Decimal(3)
    assert sol.max_drawdown == Decimal(3)


def test_open_research_and_incomplete_rows_do_not_change_the_book() -> None:
    trades = _book() + [
        _trade("open-1", "BTCUSDT", "momentum", None, None),
        _trade("half", "BTCUSDT", "momentum", None, T6),
        _trade("research-1", "BTCUSDT", "momentum", "1000", T6, mode="research"),
    ]
    analysis, report = _paper(trades)
    assert analysis.excluded_open == 1
    assert analysis.excluded_incomplete == 1
    assert analysis.excluded_research == 1
    assert report.sample_size == 6
    assert report.net_pnl == Decimal(12)
    assert report.max_drawdown == Decimal(9)


def test_shadow_book_is_separate_from_paper() -> None:
    trades = _book() + [
        _trade("inr-1", "RELIANCE", "opening-range", "67.50", T6, mode="shadow", currency="INR"),
    ]
    analysis = compute_outcomes(trades)
    assert [(item.mode, item.currency) for item in analysis.reports] == [
        ("paper", "USDT"),
        ("shadow", "INR"),
    ]
    paper = analysis.reports[0]
    shadow = analysis.reports[1]
    assert paper.max_drawdown == Decimal(9)
    assert paper.sample_size == 6
    assert shadow.sample_size == 1
    assert shadow.net_pnl == Decimal("67.50")
    assert shadow.win_rate == Decimal(1)
    assert shadow.average_win == Decimal("67.50")
    assert shadow.average_loss is None
    assert shadow.profit_factor is None
    assert shadow.max_drawdown == Decimal(0)
    assert shadow.expectancy == Decimal("67.50")


def test_same_close_time_orders_by_id() -> None:
    """id `a` then id `b`. Equity +5 then 0, so the drawdown is 5 and the
    current streak is a loss. The other order would end on a win.
    """
    trades = [
        _trade("b", "BTCUSDT", "momentum", "-5", T1),
        _trade("a", "BTCUSDT", "momentum", "5", T1),
    ]
    _analysis, report = _paper(trades)
    assert report.max_drawdown == Decimal(5)
    assert report.current_streak_kind == "loss"
    assert report.current_streak_length == 1
    assert report.longest_win_streak == 1
    assert report.longest_loss_streak == 1
    assert report.net_pnl == Decimal(0)


def test_breakeven_stays_in_the_sample_and_breaks_the_streak() -> None:
    trades = [
        _trade("w", "BTCUSDT", "momentum", "10", T1),
        _trade("z", "BTCUSDT", "momentum", "0", T2),
    ]
    _analysis, report = _paper(trades)
    assert report.sample_size == 2
    assert report.wins == 1 and report.losses == 0 and report.breakeven == 1
    assert report.win_rate == Decimal(1) / Decimal(2)
    assert report.average_win == Decimal(10)
    assert report.average_loss is None
    assert report.expectancy == Decimal(5)
    assert report.profit_factor is None
    assert report.max_drawdown == Decimal(0)
    assert report.longest_win_streak == 1
    assert report.longest_loss_streak == 0
    assert report.current_streak_kind == "none"
    assert report.current_streak_length == 0


def test_thirty_trades_is_not_a_small_sample_and_twenty_nine_is() -> None:
    full = [
        _trade(f"d{i:02d}", "BTCUSDT", "momentum", "1", f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 31)
    ]
    short = full[:-1]
    _full_analysis, full_report = _paper(full)
    _short_analysis, short_report = _paper(short)

    assert full_report.sample_size == 30
    assert full_report.small_sample is False
    assert full_report.win_rate == Decimal(1)
    assert full_report.expectancy == Decimal(1)
    assert full_report.average_win == Decimal(1)
    assert full_report.average_loss is None
    assert full_report.profit_factor is None
    assert full_report.max_drawdown == Decimal(0)
    assert full_report.net_pnl == Decimal(30)
    assert full_report.longest_win_streak == 30
    assert full_report.current_streak_length == 30

    assert short_report.sample_size == 29
    assert short_report.small_sample is True
    assert short_report.net_pnl == Decimal(29)
    assert short_report.longest_win_streak == 29


def test_crypto_trades_fixture_matches_the_one_closed_fill() -> None:
    """Committed fixture: one closed pnl of -12.40 and one still open.

    The page has a next cursor, so the sample is marked incomplete. The open
    row is not a result. Equity goes from 0 to -12.40, so the drawdown is 12.40.
    """
    payload = json.loads((FIXTURES / "crypto" / "trades.json").read_text(encoding="utf-8"))
    analysis = compute_outcomes(payload["data"]["items"], truncated=True)
    assert analysis.excluded_open == 1
    assert analysis.truncated is True
    report = analysis.reports[0]
    assert report.sample_size == 1
    assert report.wins == 0 and report.losses == 1
    assert report.win_rate == Decimal(0)
    assert report.average_win is None
    assert report.average_loss == Decimal("12.40")
    assert report.expectancy == Decimal("-12.40")
    assert report.profit_factor == Decimal(0)
    assert report.net_pnl == Decimal("-12.40")
    assert report.max_drawdown == Decimal("12.40")
    assert report.longest_loss_streak == 1
    assert report.current_streak_kind == "loss"
    assert report.small_sample is True
    assert report.by_symbol[0].key == "BTCUSDT"
    assert report.by_strategy[0].key == "momentum"


def test_sheet_spells_the_hand_figures_and_narration_must_quote_them() -> None:
    analysis, report = _paper(_book())
    sheet = render_analysis(analysis)
    assert f"win_rate: {format(report.win_rate, 'f')}" in sheet
    assert f"profit_factor: {format(report.profit_factor, 'f')}" in sheet
    assert "sample_size: 6" in sheet
    assert "period_start: 2026-11-01T00:00:00Z" in sheet
    assert "period_end: 2026-11-06T00:00:00Z" in sheet
    assert WARNING in sheet
    assert "average_loss: undefined" in sheet.split("symbol.BTCUSDT.average_loss: undefined")[-1] or (
        "symbol.BTCUSDT.average_loss: undefined" in sheet
    )

    good = "\n".join(
        [
            "Sample size: 6",
            "Period: 2026-11-01T00:00:00Z to 2026-11-06T00:00:00Z",
            WARNING,
            f"Win rate: {format(report.win_rate, 'f')}. Expectancy: {format(report.expectancy, 'f')}.",
            f"Max drawdown: {format(report.max_drawdown, 'f')}.",
        ]
    )
    assert narration_problems(good, [sheet]) == []

    invented = good.replace(format(report.win_rate, "f"), "0.99")
    problems = narration_problems(invented, [sheet])
    assert any("0.99" in problem for problem in problems)

    missing_warning = good.replace(WARNING, "")
    assert any("small-sample" in problem for problem in narration_problems(missing_warning, [sheet]))


def test_thirty_trade_narration_must_not_warn() -> None:
    trades = [
        _trade(f"d{i:02d}", "BTCUSDT", "momentum", "1", f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 31)
    ]
    analysis = compute_outcomes(trades)
    sheet = render_analysis(analysis)
    assert WARNING not in sheet
    text = "\n".join(
        [
            "Sample size: 30",
            "Period: 2026-01-01T00:00:00Z to 2026-01-30T00:00:00Z",
            "Expectancy: 1.",
        ]
    )
    assert narration_problems(text, [sheet]) == []
    warned = text + "\n" + WARNING
    assert any("30 or more" in problem for problem in narration_problems(warned, [sheet]))


def test_analytics_modules_do_not_call_a_model() -> None:
    root = Path("app/analytics")
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "EngineRouter" not in text
    assert "openai" not in text
    assert "httpx" not in text


class _TradesFeed:
    def __init__(self, read: TradingRead) -> None:
        self.read = read
        self.kwargs: dict | None = None

    async def trades(self, **kwargs):
        self.kwargs = kwargs
        return self.read


def test_outcomes_tool_requests_one_page_and_quotes_the_sheet(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_ENGINE_BASE_URL", raising=False)
    payload = {
        "source_engine": "crypto-signal-engine",
        "schema_version": "1.0",
        "generated_at": T6,
        "data_as_of": T6,
        "status": "ok",
        "is_stale": False,
        "mode": "paper",
        "correlation_id": "corr-outcomes",
        "data": {"items": _book(), "next_cursor": None},
        "errors": [],
    }
    envelope = TradeResponse.model_validate(payload)
    feed = _TradesFeed(
        TradingRead(
            adapter="crypto-signal-engine",
            outcome=Outcome.OK,
            stale=False,
            correlation_id="corr-outcomes",
            envelope=envelope,
        )
    )
    set_trading_adapter("crypto", feed)
    try:
        import asyncio

        output = asyncio.run(trading_crypto_outcomes())
    finally:
        set_trading_adapter("crypto", None)

    assert feed.kwargs == {"limit": 500}
    assert "sample_size: 6" in output
    assert "max_drawdown: 9" in output
    assert "trading-v1" in output
    assert "source_engine: crypto-signal-engine" in output
    assert INCOMPLETE not in output


async def test_runner_rejects_a_recomputed_win_rate_then_accepts_the_sheet(agents) -> None:
    payload = {
        "source_engine": "crypto-signal-engine",
        "schema_version": "1.0",
        "generated_at": T6,
        "data_as_of": T6,
        "status": "ok",
        "is_stale": False,
        "mode": "paper",
        "correlation_id": "corr-outcomes",
        "data": {"items": _book(), "next_cursor": None},
        "errors": [],
    }
    envelope = TradeResponse.model_validate(payload)
    set_trading_adapter(
        "crypto",
        _TradesFeed(
            TradingRead(
                adapter="crypto-signal-engine",
                outcome=Outcome.OK,
                stale=False,
                correlation_id="corr-outcomes",
                envelope=envelope,
            )
        ),
    )
    try:
        preview = await trading_crypto_outcomes()
        evidence = evidence_from_output(preview)
        assert evidence is not None
        good = "\n".join(
            [
                f"SOURCE: {evidence.source_engine}",
                f"DATA_AS_OF: {evidence.data_as_of}",
                "HOLDINGS: none",
                "",
                "Sample size: 6",
                "Period: 2026-11-01T00:00:00Z to 2026-11-06T00:00:00Z",
                WARNING,
                "Win rate: 0.5. Expectancy: 2. Max drawdown: 9.",
                "",
                "## Assumptions",
                "The outcomes sheet is the only source for these figures.",
                "",
                "## Uncertainty",
                "Six trades is a small sample.",
            ]
        )
        bad = good.replace("Win rate: 0.5.", "Win rate: 0.99.")
        router, _engine = scripted_router([
            tool_result("trading.crypto.outcomes", {}),
            text_result(bad),
            text_result(good),
        ])
        result = await AgentRunner(router).run(
            agents.get("crypto_analyst"), objective="Narrate the paper book."
        )
    finally:
        set_trading_adapter("crypto", None)

    assert result.accepted is True
    assert "0.99" not in result.text
    assert "Sample size: 6" in result.text
    assert "Period: 2026-11-01T00:00:00Z to 2026-11-06T00:00:00Z" in result.text
    assert WARNING in result.text


def test_missing_engine_does_not_invent_a_win_rate(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_ENGINE_BASE_URL", raising=False)
    set_trading_adapter("crypto", None)
    import asyncio

    output = asyncio.run(trading_crypto_outcomes())
    assert "status: unavailable" in output
    assert "win_rate:" not in output
    assert narration_problems("Win rate: 0.5", [output])
