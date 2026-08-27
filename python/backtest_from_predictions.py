"""Turn stored out-of-sample predictions into a return series and risk metrics.

Phase D. This is a measurement instrument, not a strategy. The model currently
has no directional edge (ROC-AUC ~0.51), so the expected result is that the
threshold rule earns nothing after costs -- and that is the deliverable. A
backtest that can only report success cannot tell you when you have failed, and
an honest negative result here is what makes a later positive one believable.

Two conventions follow from that:

  * Every strategy is scored against `always_long`, which holds the universe
    unconditionally. An absolute return says nothing -- 8% in a year the market
    returned 12% is a losing strategy. Edge is a difference.

  * Costs are charged on position *changes* and stored per day alongside the
    turnover that produced them, so the deduction can be re-derived rather than
    trusted (see sql/90_assertions.sql).

Point-in-time safety is inherited rather than re-established: model_predictions
holds only out-of-sample rows (asserted at the database level), and a position
taken on `trading_date` earns `ret_fwd_1d`, the t -> t+1 return. Nothing here
looks at a price the position could not have been sized on.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg


ROOT = Path(__file__).resolve().parents[1]
DBNAME = "edmp_engine"

TRADING_DAYS_PER_YEAR = 252

# Round-trip cost per unit of position change, in basis points. Commission and
# slippage combined: for liquid ETFs a marketable order gives up roughly half a
# spread, and these names quote a cent or two on prices in the tens to hundreds.
# Deliberately a single knob rather than a spread model -- a precise-looking cost
# model resting on invented parameters is less honest than one obvious number
# that can be varied with --cost-bps.
DEFAULT_COST_BPS = 1.5

# p_large_move above this goes flat.
#
# Scaled to the label, which is the thing that makes this number defensible.
# y_large_move_next fires when |ret_fwd_1d| > 2 * vol_20d, so for roughly normal
# returns its unconditional rate is P(|z| > 2) ~ 4.6%; fat tails push the observed
# rate to ~6.7%. A calibrated model therefore emits probabilities clustered around
# 0.07, and in practice p_large_move spans 0.002 to 0.36. 0.10 is about 1.5x the
# unconditional rate: "the model thinks a large move is materially more likely
# than usual", which fires on roughly 9% of asset-days -- often enough to be a
# real test, rare enough to still mean something.
#
# The first version of this constant was 0.60, borrowed by analogy from
# LONG_THRESHOLD. That threshold governs y_up_next_day, whose base rate is ~53%,
# so it was off by an order of magnitude relative to this label and never fired:
# the strategy silently reduced to always_long and reported an edge of exactly
# +0.0000 against it. Fixing an inoperative rule is not the same as tuning one --
# no working threshold had been observed, so no result influenced this choice,
# and it must stay that way. Searching values until the equity curve improves is
# fitting the test set, the trap docs/design_decisions.md §11 refuses for
# hyperparameters. Deriving it instead from each fold's *training* predictions
# would be stricter still, and is recorded as deferred.
LARGE_MOVE_EXIT = 0.10

# Window for the rolling columns. ~3 months: long enough for a Sharpe estimate to
# mean anything, short enough to still show regime changes.
ROLLING_WINDOW = 60


def get_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def load_predictions(conn: psycopg.Connection, model_run_ids: list[int] | None) -> pd.DataFrame:
    """Predictions joined to the realised forward return they were a bet on.

    ret_fwd_1d comes from labels_daily rather than being recomputed here, so the
    backtest and the training labels cannot drift apart: sql/90_assertions.sql
    already pins ret_fwd_1d(t) == ret_1d(t+1).

    An inner join is deliberate. A prediction whose label is missing (the last
    row of an asset's history has no next day) must drop out rather than be
    filled with a zero return, which would silently dilute every metric.
    """
    sql = """
        SELECT p.model_run_id, p.asset_id, a.symbol, p.trading_date,
               p.p_up, p.p_large_move, p.signal, l.ret_fwd_1d
        FROM analytics.model_predictions p
        JOIN analytics.labels_daily l
          ON l.asset_id = p.asset_id AND l.trading_date = p.trading_date
        JOIN staging.assets a ON a.asset_id = p.asset_id
        WHERE l.ret_fwd_1d IS NOT NULL
    """
    params: list = []
    if model_run_ids:
        sql += " AND p.model_run_id = ANY(%s)"
        params.append(model_run_ids)
    sql += " ORDER BY p.model_run_id, p.asset_id, p.trading_date"
    return pd.read_sql_query(sql, conn, params=params or None)


# --- Position rules -------------------------------------------------------
#
# Each maps a frame of predictions to a position per asset-day in [-1, 1].
# Pure functions of columns available at `trading_date`, so none can look ahead.


def position_direction_threshold(df: pd.DataFrame) -> pd.Series:
    """The roadmap's rule: long above 0.55, short below 0.45, else flat.

    Reads the stored `signal` rather than re-deriving it from p_up, so the
    backtest scores the decision the model actually emitted. Re-deriving would
    let a later threshold change silently rewrite history.
    """
    return df["signal"].astype(float)


def position_large_move_filter(df: pd.DataFrame) -> pd.Series:
    """Hold the market, step aside when a large move looks likely.

    This is the only rule that uses the signal the model actually has:
    y_large_move_next carries a real edge (~0.57) while direction does not. It is
    a risk filter, not a forecast -- it never expresses a view on which way.

    Note it is as likely to miss upside as downside, since the label is
    unsigned. Whether that is worth paying for is exactly what the backtest is
    being asked, rather than assumed.
    """
    return np.where(df["p_large_move"] > LARGE_MOVE_EXIT, 0.0, 1.0)


def position_always_long(df: pd.DataFrame) -> pd.Series:
    """The benchmark every other strategy is measured against."""
    return pd.Series(1.0, index=df.index)


STRATEGIES = {
    "direction_threshold": position_direction_threshold,
    "large_move_filter": position_large_move_filter,
    "always_long": position_always_long,
}


# --- Return construction --------------------------------------------------


def daily_portfolio(df: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    """Per-asset positions to an equal-weight daily portfolio series.

    Turnover is |position(t) - position(t-1)| per asset, with the previous
    position taken as 0 on an asset's first day: entering from cash is a real
    trade and charging nothing for it would understate costs for every strategy
    that holds a position throughout.
    """
    df = df.sort_values(["asset_id", "trading_date"]).copy()

    prev = df.groupby("asset_id")["position"].shift(1).fillna(0.0)
    df["turnover"] = (df["position"] - prev).abs()
    df["gross"] = df["position"] * df["ret_fwd_1d"]
    df["net"] = df["gross"] - df["turnover"] * cost_bps / 10_000.0
    df["is_active"] = df["position"] != 0.0

    daily = (
        df.groupby("trading_date")
        .agg(
            avg_position=("position", "mean"),
            turnover=("turnover", "mean"),
            gross_return=("gross", "mean"),
            net_return=("net", "mean"),
            n_active=("is_active", "sum"),
            n_trades=("turnover", lambda s: int((s > 0).sum())),
        )
        .reset_index()
        .sort_values("trading_date")
    )

    # Compounded, not summed: a return series that is added up overstates gains
    # and understates the drawdowns that matter.
    equity = (1.0 + daily["net_return"]).cumprod()
    daily["cum_return"] = equity - 1.0

    # The running peak is floored at the starting capital of 1.0. Without that
    # floor the peak begins at the *first day's* equity, so a decline starting on
    # day one is measured against an already-fallen high and reports as no
    # drawdown at all -- a strategy that opened -20% and clawed back to -3% would
    # show max_drawdown of 0.0. Errors of this shape always flatter the backtest,
    # which is why the number is pinned by a test.
    peak = equity.cummax().clip(lower=1.0)
    daily["drawdown"] = equity / peak - 1.0

    daily["hit_rate_rolling"] = (
        (daily["net_return"] > 0).rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).mean()
    )
    rolling_mean = daily["net_return"].rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).mean()
    rolling_std = daily["net_return"].rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).std()
    daily["sharpe_rolling"] = np.sqrt(TRADING_DAYS_PER_YEAR) * rolling_mean / rolling_std.replace(0, np.nan)

    return daily


def summarize(daily: pd.DataFrame) -> dict:
    """Whole-period metrics for one (model run, strategy, cost).

    Hit rate and expectancy are computed over days the portfolio actually had
    exposure. Counting flat days as losses would make an abstaining strategy look
    terrible and an always-invested one look disciplined, when the difference is
    only how often each chose to trade.
    """
    net = daily["net_return"].to_numpy(dtype=float)
    active = daily[daily["n_active"] > 0]["net_return"].to_numpy(dtype=float)

    std = net.std(ddof=1) if len(net) > 1 else 0.0
    sharpe = float(np.sqrt(TRADING_DAYS_PER_YEAR) * net.mean() / std) if std > 0 else None

    wins = active[active > 0]
    losses = active[active < 0]
    hit_rate = float(len(wins) / len(active)) if len(active) else None

    # Expectancy in return units per active day. Hit rate alone misleads when
    # wins and losses are different sizes; this is the number that says whether
    # the strategy makes money on average.
    if hit_rate is not None and len(wins) and len(losses):
        expectancy = float(hit_rate * wins.mean() - (1 - hit_rate) * abs(losses.mean()))
    else:
        expectancy = None

    return {
        "n_days": int(len(daily)),
        "n_trades": int(daily["n_trades"].sum()),
        "total_return": float(daily["cum_return"].iloc[-1]) if len(daily) else None,
        "sharpe": sharpe,
        "max_drawdown": float(daily["drawdown"].min()) if len(daily) else None,
        "hit_rate": hit_rate,
        "expectancy": expectancy,
    }


# --- Persistence ----------------------------------------------------------


def write_backtest(
    conn: psycopg.Connection,
    model_run_id: int,
    strategy: str,
    cost_bps: float,
    metrics: dict,
    daily: pd.DataFrame,
    git_commit: str | None,
) -> int:
    """Append one backtest_runs row and its daily series.

    Append-only, like the model tables and for the same reason: comparing runs is
    the point, so nothing here overwrites. See docs/design_decisions.md §2.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics.backtest_runs
                (model_run_id, strategy, cost_bps, n_days, n_trades, total_return,
                 sharpe, max_drawdown, hit_rate, expectancy, git_commit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING backtest_run_id
            """,
            (
                model_run_id, strategy, cost_bps, metrics["n_days"], metrics["n_trades"],
                metrics["total_return"], metrics["sharpe"], metrics["max_drawdown"],
                metrics["hit_rate"], metrics["expectancy"], git_commit,
            ),
        )
        backtest_run_id = cur.fetchone()[0]

        # NaN is not NULL to psycopg; the rolling columns are undefined for the
        # first ROLLING_WINDOW rows and must land as NULL rather than as a value
        # that arithmetic silently propagates.
        rows = [
            (
                backtest_run_id, r.trading_date,
                _none_if_nan(r.avg_position), _none_if_nan(r.turnover),
                _none_if_nan(r.gross_return), _none_if_nan(r.net_return),
                _none_if_nan(r.cum_return), _none_if_nan(r.drawdown),
                _none_if_nan(r.hit_rate_rolling), _none_if_nan(r.sharpe_rolling),
            )
            for r in daily.itertuples()
        ]
        cur.executemany(
            """
            INSERT INTO analytics.backtest_results
                (backtest_run_id, trading_date, avg_position, turnover, gross_return,
                 net_return, cum_return, drawdown, hit_rate_rolling, sharpe_rolling)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    return backtest_run_id


def _none_if_nan(value) -> float | None:
    return None if value is None or (isinstance(value, float) and np.isnan(value)) else float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest stored out-of-sample predictions.")
    parser.add_argument(
        "--model-run-ids", type=int, nargs="*", default=None,
        help="Model runs to backtest. Default: every run in the warehouse, which after a "
             "rebuild is exactly the latest training invocation (see design_decisions.md §2).",
    )
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS,
                        help=f"Cost per unit of position change (default {DEFAULT_COST_BPS}).")
    parser.add_argument("--strategies", nargs="*", default=list(STRATEGIES),
                        choices=list(STRATEGIES), help="Position rules to run.")
    args = parser.parse_args()

    with psycopg.connect(dbname=DBNAME) as conn:
        preds = load_predictions(conn, args.model_run_ids)
        if preds.empty:
            raise RuntimeError(
                "No predictions found. Run `make train_baseline` first -- and note that a "
                "warehouse rebuild clears model_runs, so training must come after it."
            )

        git_commit = get_git_commit()
        summaries: list[dict] = []

        # Each model run is backtested separately. Folds are separated by embargo
        # gaps, so concatenating them into one equity curve would compound across
        # periods the strategy was never in the market for.
        with conn.transaction():
            for model_run_id, run_preds in preds.groupby("model_run_id"):
                for strategy in args.strategies:
                    scored = run_preds.copy()
                    scored["position"] = STRATEGIES[strategy](scored)
                    daily = daily_portfolio(scored, args.cost_bps)
                    metrics = summarize(daily)
                    backtest_run_id = write_backtest(
                        conn, int(model_run_id), strategy, args.cost_bps, metrics, daily, git_commit
                    )
                    summaries.append(
                        {"model_run_id": int(model_run_id), "strategy": strategy,
                         "backtest_run_id": backtest_run_id, **metrics}
                    )

        print_report(pd.DataFrame(summaries), args.cost_bps)


def print_report(summary: pd.DataFrame, cost_bps: float) -> None:
    """Per-fold detail, then the comparison that actually answers the question."""
    def fmt(v, spec=".4f"):
        return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else format(v, spec)

    print(f"\nBacktest at {cost_bps} bps per unit of position change\n")
    for model_run_id, block in summary.groupby("model_run_id"):
        print(f"model_run_id={model_run_id}")
        for r in block.itertuples():
            print(
                f"  {r.strategy:<20} sharpe={fmt(r.sharpe):>8}  total={fmt(r.total_return):>8}  "
                f"maxDD={fmt(r.max_drawdown):>8}  hit={fmt(r.hit_rate):>7}  trades={r.n_trades}"
            )
        print()

    print("Mean across folds (the spread across folds is the result, not any single one):")
    agg = summary.groupby("strategy").agg(
        sharpe=("sharpe", "mean"), total_return=("total_return", "mean"),
        max_drawdown=("max_drawdown", "mean"), hit_rate=("hit_rate", "mean"),
        trades=("n_trades", "sum"),
    )
    for strategy, r in agg.iterrows():
        print(
            f"  {strategy:<20} sharpe={fmt(r.sharpe):>8}  total={fmt(r.total_return):>8}  "
            f"maxDD={fmt(r.max_drawdown):>8}  hit={fmt(r.hit_rate):>7}  trades={int(r.trades)}"
        )

    # The only comparison that means anything: a strategy is worth something only
    # relative to holding the market, never in absolute terms.
    if "always_long" in agg.index:
        benchmark = agg.loc["always_long"]
        print("\nVersus always_long:")
        for strategy, r in agg.iterrows():
            if strategy == "always_long":
                continue
            d_sharpe = (r.sharpe - benchmark.sharpe) if pd.notna(r.sharpe) and pd.notna(benchmark.sharpe) else None
            d_total = (r.total_return - benchmark.total_return) if pd.notna(r.total_return) else None
            print(f"  {strategy:<20} d_sharpe={fmt(d_sharpe, '+.4f'):>9}  d_total={fmt(d_total, '+.4f'):>9}")


if __name__ == "__main__":
    main()
