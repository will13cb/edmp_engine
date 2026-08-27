"""Invariants for the backtest engine.

A backtest is the easiest place in this project to fool yourself: every bug makes
the equity curve prettier rather than raising anything. Uncharged entry costs,
summed instead of compounded returns, a hit rate that quietly counts abstentions
as wins -- each produces a plausible number, which is the failure mode this
codebase keeps running into.

These tests pin the arithmetic against hand-computed values. Pure: no database.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from backtest_from_predictions import (
    LARGE_MOVE_EXIT,
    daily_portfolio,
    position_always_long,
    position_direction_threshold,
    position_large_move_filter,
    summarize,
)


def frame(positions: list[float], returns: list[float], asset_id: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "asset_id": asset_id,
            "trading_date": [date(2026, 1, d + 1) for d in range(len(positions))],
            "position": positions,
            "ret_fwd_1d": returns,
        }
    )


def test_entering_from_cash_is_charged():
    """An asset's first day has no previous row, and treating that as "no change"
    would hand every buy-and-hold strategy a free entry. Previous position is 0,
    so opening a full position is a full unit of turnover."""
    daily = daily_portfolio(frame([1.0, 1.0], [0.0, 0.0]), cost_bps=10.0)

    assert daily["turnover"].iloc[0] == 1.0
    assert daily["turnover"].iloc[1] == 0.0


def test_cost_is_charged_on_change_not_on_holding():
    """Holding costs nothing; flipping costs. A model that trades constantly must
    be penalised against one that does not, or turnover is free."""
    daily = daily_portfolio(frame([1.0, 1.0, -1.0], [0.0, 0.0, 0.0]), cost_bps=10.0)

    # 10bp = 0.001 per unit of turnover. Day 3 flips +1 -> -1, two units.
    assert daily["net_return"].iloc[0] == -0.001
    assert daily["net_return"].iloc[1] == 0.0
    assert daily["net_return"].iloc[2] == -0.002


def test_net_return_is_recoverable_from_stored_columns():
    """The stored series must let the cost deduction be re-derived, which is what
    the database assertion checks. If this identity does not hold, an incorrect
    cost model is invisible."""
    daily = daily_portfolio(frame([1.0, 0.0, 1.0], [0.02, -0.01, 0.03]), cost_bps=5.0)

    expected = daily["gross_return"] - daily["turnover"] * 5.0 / 10_000.0
    assert np.allclose(daily["net_return"], expected)


def test_returns_compound_rather_than_sum():
    """Summing overstates gains and understates drawdowns. Two +10% days are
    +21%, not +20%."""
    daily = daily_portfolio(frame([1.0, 1.0], [0.10, 0.10]), cost_bps=0.0)

    assert np.isclose(daily["cum_return"].iloc[-1], 0.21)


def test_drawdown_is_measured_from_the_running_peak():
    """+10% then -10% ends below water: 1.10 * 0.90 = 0.99, a 10% fall from the
    peak. A drawdown computed from the start instead of the peak would report
    -1%, understating the loss actually experienced."""
    daily = daily_portfolio(frame([1.0, 1.0], [0.10, -0.10]), cost_bps=0.0)

    assert daily["drawdown"].iloc[0] == 0.0
    assert np.isclose(daily["drawdown"].iloc[1], -0.10)
    assert (daily["drawdown"] <= 0).all()


def test_positions_are_equal_weighted_across_assets():
    """Portfolio return is the mean across assets holding that day, not the sum,
    or a larger universe would inflate returns for free."""
    a = frame([1.0], [0.10], asset_id=1)
    b = frame([1.0], [0.20], asset_id=2)
    daily = daily_portfolio(pd.concat([a, b], ignore_index=True), cost_bps=0.0)

    assert np.isclose(daily["gross_return"].iloc[0], 0.15)


def test_hit_rate_ignores_days_with_no_exposure():
    """Counting flat days as losses would make an abstaining strategy look far
    worse than it is, and would flatter one that is always invested. Only days
    the portfolio actually had a position can be won or lost."""
    daily = daily_portfolio(frame([1.0, 0.0, 0.0, 1.0], [0.01, 0.05, -0.05, 0.01]), cost_bps=0.0)
    metrics = summarize(daily)

    # Two active days, both profitable -- the two flat days are not losses.
    assert metrics["hit_rate"] == 1.0


def test_max_drawdown_is_the_worst_trough_not_the_last():
    """Reporting the final drawdown would hide a deep recovery mid-period, which
    is the number a risk limit actually cares about."""
    daily = daily_portfolio(frame([1.0, 1.0, 1.0], [-0.20, 0.10, 0.10]), cost_bps=0.0)
    metrics = summarize(daily)

    assert np.isclose(metrics["max_drawdown"], -0.20)
    assert metrics["max_drawdown"] <= daily["drawdown"].iloc[-1]


def test_sharpe_is_none_when_returns_do_not_vary():
    """A constant series has zero volatility, and dividing by it would yield inf
    or nan -- a headline metric that is not a number, reported as though it
    were."""
    daily = daily_portfolio(frame([1.0, 1.0, 1.0], [0.0, 0.0, 0.0]), cost_bps=0.0)

    assert summarize(daily)["sharpe"] is None


def test_always_long_holds_the_universe_unconditionally():
    """The benchmark must not depend on any model output, or comparisons against
    it stop being comparisons against the market."""
    df = pd.DataFrame({"p_up": [0.9, 0.1], "p_large_move": [0.9, 0.01], "signal": [1, -1]})

    assert (position_always_long(df) == 1.0).all()


def test_large_move_filter_steps_aside_only_above_the_threshold():
    df = pd.DataFrame({"p_large_move": [LARGE_MOVE_EXIT - 0.01, LARGE_MOVE_EXIT + 0.01]})

    assert list(position_large_move_filter(df)) == [1.0, 0.0]


def test_large_move_threshold_is_on_the_scale_of_its_label():
    """Regression test for a rule that silently did nothing.

    LARGE_MOVE_EXIT was once 0.60, borrowed from LONG_THRESHOLD, which governs a
    label with a ~53% base rate. y_large_move_next fires on |ret| > 2*vol, so its
    unconditional rate is nearer 5-7% and a calibrated model never emits 0.60.
    The strategy silently collapsed into always_long and reported an edge of
    exactly zero against it. A threshold above 0.5 for this label can only be a
    scale error.
    """
    assert 0.0 < LARGE_MOVE_EXIT < 0.5


def test_direction_threshold_uses_the_stored_signal():
    """Scoring the decision the model actually emitted, rather than re-deriving
    it, so a later threshold change cannot rewrite past backtests."""
    df = pd.DataFrame({"signal": [1, 0, -1], "p_up": [0.99, 0.99, 0.99]})

    assert list(position_direction_threshold(df)) == [1.0, 0.0, -1.0]
