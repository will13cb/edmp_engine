"""Render the project's results as figures, straight from the warehouse.

Every finding in this repository is currently a table or a line of stdout, which
means the results are only legible to someone willing to build the warehouse and
run the pipeline. These four charts are the same numbers, readable in ten
seconds.

They are generated rather than exported by hand for the same reason the pipeline
rebuilds rather than updates incrementally: a figure that needs manual
re-exporting is a figure that will silently go stale the moment calibration or
event features move the numbers. `make charts` regenerates all of them.

Reads only. Nothing here writes to the database.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

# Chosen before pyplot is imported: this runs headless in CI and over SSH, where
# the default interactive backend would fail to find a display.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
from sklearn.metrics import roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "img"
DBNAME = "edmp_engine"

# The cost assumption the headline results are quoted at. Charts fix it rather
# than mixing assumptions on one axis; the sensitivity across costs is a
# separate question, reported in design_decisions.md §10.
COST_BPS = 1.5

# Equal-count bins; see chart_calibration for why not fixed-width.
N_CALIBRATION_BINS = 10

# Colour-blind-safe, and deliberately assigned: the benchmark is the reference
# line every other series is judged against, so it reads darker and heavier.
COLORS = {
    "always_long": "#333333",
    "large_move_filter": "#0072B2",
    "direction_threshold": "#D55E00",
}
LABELS = {
    "always_long": "always_long (benchmark)",
    "large_move_filter": "large_move_filter",
    "direction_threshold": "direction_threshold",
}


def latest_backtests(conn: psycopg.Connection) -> pd.DataFrame:
    """Most recent backtest per (model run, strategy) at the headline cost.

    backtest_runs is append-only, so re-running the backtest leaves several rows
    per group. Taking the max backtest_run_id keeps the newest without deleting
    history — the earlier rows are a record of what was run, not stale data to
    be cleaned up.
    """
    return pd.read_sql_query(
        """
        WITH newest AS (
            SELECT DISTINCT ON (model_run_id, strategy)
                   backtest_run_id, model_run_id, strategy, sharpe, max_drawdown
            FROM analytics.backtest_runs
            WHERE cost_bps = %(cost)s
            ORDER BY model_run_id, strategy, backtest_run_id DESC
        )
        SELECT n.*, r.trading_date, r.cum_return, r.drawdown
        FROM newest n
        JOIN analytics.backtest_results r USING (backtest_run_id)
        ORDER BY n.strategy, r.trading_date
        """,
        conn,
        params={"cost": COST_BPS},
    )


def load_predictions(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT p.model_run_id, a.symbol, p.trading_date, p.p_up, p.p_large_move,
               l.y_up_next_day, l.y_large_move_next
        FROM analytics.model_predictions p
        JOIN analytics.labels_daily l
          ON l.asset_id = p.asset_id AND l.trading_date = p.trading_date
        JOIN staging.assets a ON a.asset_id = p.asset_id
        WHERE l.y_up_next_day IS NOT NULL AND l.y_large_move_next IS NOT NULL
        """,
        conn,
    )


def save(fig: plt.Figure, name: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {path.relative_to(ROOT)}")


def chart_equity(bt: pd.DataFrame) -> None:
    """Cumulative return per strategy, one segment per walk-forward fold.

    The folds are plotted as separate segments on a shared time axis rather than
    stitched into one curve. They are genuinely separate backtests, each
    starting from zero, and the gaps between them are the embargo periods -- a
    continuous line would compound across stretches the strategy was never
    invested in.
    """
    fig, ax = plt.subplots(figsize=(11, 5))

    for strategy, block in bt.groupby("strategy"):
        for i, (_, fold) in enumerate(block.groupby("model_run_id")):
            ax.plot(
                fold["trading_date"], fold["cum_return"] * 100,
                color=COLORS[strategy], linewidth=1.9,
                alpha=0.9 if strategy == "always_long" else 0.85,
                label=LABELS[strategy] if i == 0 else None,
            )

    ax.axhline(0, color="#999999", linewidth=0.8, linestyle="--", zorder=0)
    ax.set_title("Cumulative return by strategy, per walk-forward fold\n"
                 f"({COST_BPS} bps per unit of position change; each fold is its own backtest)",
                 fontsize=12, loc="left")
    ax.set_ylabel("Cumulative return (%)")
    ax.legend(frameon=False, loc="upper left", fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    save(fig, "equity_curves.png")


def chart_drawdown(bt: pd.DataFrame) -> None:
    """Drawdown alongside the equity curve, because return without risk is half a result."""
    fig, ax = plt.subplots(figsize=(11, 3.6))

    # Lines rather than filled areas: three overlapping translucent fills turn to
    # mud, and the shallowest series (direction_threshold, which is mostly out of
    # the market) disappears underneath the others exactly where it matters.
    for strategy, block in bt.groupby("strategy"):
        for i, (_, fold) in enumerate(block.groupby("model_run_id")):
            ax.plot(
                fold["trading_date"], fold["drawdown"] * 100,
                color=COLORS[strategy], linewidth=1.4, alpha=0.9,
                label=LABELS[strategy] if i == 0 else None,
            )

    ax.set_title("Drawdown from running peak", fontsize=12, loc="left")
    ax.set_ylabel("Drawdown (%)")
    ax.legend(frameon=False, loc="lower left", fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    save(fig, "drawdown.png")


def chart_calibration(preds: pd.DataFrame) -> None:
    """Predicted probability against realised frequency.

    The diagonal is perfect calibration. Points below it are overconfidence: the
    model says 0.58 and the outcome happens less often than that. This is the
    chart that motivates the calibration work, and it is deliberately drawn
    before any calibrator is fitted so the improvement is visible later.
    """
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))

    panels = [
        ("p_up", "y_up_next_day", "Direction  (p_up)", COLORS["direction_threshold"]),
        ("p_large_move", "y_large_move_next", "Large move  (p_large_move)", COLORS["large_move_filter"]),
    ]

    for ax, (pcol, ycol, title, color) in zip(axes, panels):
        p = preds[pcol].to_numpy()
        y = preds[ycol].astype(int).to_numpy()

        # Equal-count (decile) bins rather than fixed-width ones. Both models
        # emit probabilities over a narrow range -- p_large_move barely exceeds
        # 0.2 -- so fixed 0.05 buckets collapse to three or four points and hide
        # the shape. Equal counts also mean every point rests on the same number
        # of observations, so none needs discounting against the others.
        bucket = pd.qcut(p, q=N_CALIBRATION_BINS, labels=False, duplicates="drop")

        xs, ys = [], []
        for b in sorted(set(bucket)):
            mask = bucket == b
            xs.append(p[mask].mean())
            ys.append(y[mask].mean())

        lo = min(xs + ys) - 0.02
        hi = max(xs + ys) + 0.02
        ax.plot([lo, hi], [lo, hi], color="#999999", linestyle="--", linewidth=1,
                label="perfectly calibrated")
        ax.plot(xs, ys, "o-", color=color, markersize=6, linewidth=1.5, alpha=0.85,
                markeredgecolor="white", zorder=3,
                label=f"observed ({N_CALIBRATION_BINS} equal-count bins)")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)

        ax.set_title(title, fontsize=11, loc="left")
        ax.set_xlabel("Predicted probability")
        ax.set_ylabel("Realised frequency")
        ax.legend(frameon=False, fontsize=8, loc="lower right")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(alpha=0.25)

    fig.suptitle("Calibration: what the model says vs. what happened (uncalibrated)",
                 fontsize=12, x=0.5, y=1.02)
    save(fig, "calibration.png")


def chart_auc_by_fold(preds: pd.DataFrame) -> None:
    """Per-fold ROC-AUC for both targets.

    The spread is the result, not the mean -- a single split cannot show whether
    an edge is stable or one lucky regime. Recomputed here from stored
    predictions rather than read from a metrics table, so the chart cannot
    disagree with the warehouse.
    """
    rows = []
    for (run, _), block in preds.groupby(["model_run_id", "model_run_id"]):
        rows.append({
            "fold": run,
            "y_up_next_day": roc_auc_score(block["y_up_next_day"].astype(int), block["p_up"]),
            "y_large_move_next": roc_auc_score(
                block["y_large_move_next"].astype(int), block["p_large_move"]),
        })
    df = pd.DataFrame(rows).sort_values("fold").reset_index(drop=True)
    df["fold_n"] = range(1, len(df) + 1)

    fig, ax = plt.subplots(figsize=(8, 4.4))
    for col, color, label in [
        ("y_large_move_next", COLORS["large_move_filter"], "y_large_move_next"),
        ("y_up_next_day", COLORS["direction_threshold"], "y_up_next_day"),
    ]:
        ax.plot(df["fold_n"], df[col], "o-", color=color, linewidth=1.8, markersize=7, label=label)
        ax.axhline(df[col].mean(), color=color, linestyle=":", linewidth=1.2, alpha=0.7)

    ax.axhline(0.5, color="#999999", linestyle="--", linewidth=1)
    ax.annotate("0.5 = no signal", xy=(1.05, 0.5), xytext=(0, 4),
                textcoords="offset points", fontsize=8, color="#666666")

    ax.set_title("ROC-AUC per walk-forward fold\n(dotted lines are means; the spread is the result)",
                 fontsize=12, loc="left")
    ax.set_xlabel("Fold (expanding window)")
    ax.set_ylabel("ROC-AUC")
    ax.set_xticks(df["fold_n"])
    ax.set_xlim(0.85, len(df) + 0.15)
    ax.legend(frameon=False, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    save(fig, "auc_by_fold.png")


def main() -> None:
    with psycopg.connect(dbname=DBNAME) as conn:
        bt = latest_backtests(conn)
        preds = load_predictions(conn)

    if bt.empty:
        raise RuntimeError(
            f"No backtest results at cost_bps={COST_BPS}. Run `make train_baseline` "
            "then `make backtest` first — and note that a rebuild clears both."
        )
    if preds.empty:
        raise RuntimeError("No predictions found. Run `make train_baseline` first.")

    chart_equity(bt)
    chart_drawdown(bt)
    chart_calibration(preds)
    chart_auc_by_fold(preds)


if __name__ == "__main__":
    main()
