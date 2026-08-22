from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, roc_auc_score
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DBNAME = "edmp_engine"

FEATURE_COLUMNS = ["ret_1d", "logret_1d", "vol_20d", "mom_5d", "mom_20d", "drawdown_60d"]
TARGET_UP = "y_up_next_day"
TARGET_LARGE = "y_large_move_next"

MODEL_NAME = "logreg_walkforward"
FEATURE_SET_VERSION = "v1"

# Phase B walk-forward validation, replacing Phase A's single 80/20 split.
# One split is one sample: it cannot tell you whether a result is a stable edge
# or one lucky regime. Expanding window here, so each fold trains on everything
# from the start of history up to its own cutoff.
# See docs/course_validation_and_backtesting.md section 2.
N_FOLDS = 5
INITIAL_TRAIN_FRACTION = 0.5

# Purging/embargo. vol_20d, mom_20d and drawdown_60d are trailing windows, so a
# test row too close to the cutoff has features computed partly from
# training-period prices. Skipping the first EMBARGO_DAYS trading days after
# each cutoff guarantees every evaluated row's feature windows sit entirely on
# the test side. 60 matches the longest lookback (drawdown_60d).
EMBARGO_DAYS = 60

# p_up above LONG_THRESHOLD -> long signal, below SHORT_THRESHOLD -> short signal,
# in between -> flat/no-trade.
LONG_THRESHOLD = 0.55
SHORT_THRESHOLD = 0.45

# Width of each calibration bucket. Narrow enough to show structure, wide enough
# that buckets still hold a usable number of rows.
CALIBRATION_BIN_WIDTH = 0.05


class Fold(NamedTuple):
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def load_training_data(conn: psycopg.Connection) -> pd.DataFrame:
    # v_training_dataset already joins features + labels and drops any row
    # with a NULL in either, so no cleaning needed here.
    return pd.read_sql_query(
        "SELECT * FROM analytics.v_training_dataset ORDER BY trading_date, asset_id",
        conn,
    )


def generate_folds(
    unique_dates: list[date],
    n_folds: int,
    initial_train_fraction: float,
    embargo_days: int,
) -> list[Fold]:
    """Build expanding-window folds over the trading calendar.

    Fold i trains on everything up to its cutoff, then tests on the block of
    dates after it, minus an embargo gap. Each fold's cutoff is the previous
    fold's test-block end, so the training set grows monotonically.
    """
    n_dates = len(unique_dates)
    initial_train_end_idx = int(n_dates * initial_train_fraction)
    test_span = n_dates - initial_train_end_idx
    block_size = test_span // n_folds

    if block_size <= embargo_days:
        raise RuntimeError(
            f"embargo_days={embargo_days} leaves no test rows per fold "
            f"(block size is {block_size} trading days). Reduce n_folds or embargo_days."
        )

    folds: list[Fold] = []
    for i in range(n_folds):
        train_end_idx = initial_train_end_idx + i * block_size
        test_start_idx = train_end_idx + embargo_days + 1
        # Last fold absorbs any remainder from the integer division above.
        test_end_idx = n_dates - 1 if i == n_folds - 1 else train_end_idx + block_size

        if test_start_idx > test_end_idx:
            continue

        folds.append(
            Fold(
                index=i + 1,
                train_start=unique_dates[0],
                train_end=unique_dates[train_end_idx],
                test_start=unique_dates[test_start_idx],
                test_end=unique_dates[test_end_idx],
            )
        )

    if not folds:
        raise RuntimeError("No usable folds were generated.")

    return folds


def get_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git repo, or git isn't installed. git_commit is nullable,
        # so just leave it unset rather than failing the whole run over it.
        return None


def predict_proba_class1(clf: LogisticRegression, X: np.ndarray) -> np.ndarray:
    # Guard against sklearn ever ordering classes as [1, 0] instead of [0, 1],
    # which would silently flip which predict_proba column is "P(class=1)".
    assert clf.classes_.tolist() == [0, 1], f"unexpected classes_: {clf.classes_}"
    return clf.predict_proba(X)[:, 1]


def safe_auc(y_true: np.ndarray, p: np.ndarray) -> float | None:
    # roc_auc_score raises when a fold's test window happens to be single-class.
    try:
        return roc_auc_score(y_true, p)
    except ValueError:
        return None


def print_confusion_matrix(y_true: np.ndarray, p_up: np.ndarray) -> None:
    """Show the trade-off at the threshold actually used to trade.

    AUC measures ranking across every threshold. This shows what happens at the
    one threshold that turns into a position.
    """
    y_pred = (p_up > LONG_THRESHOLD).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()

    print(f"    confusion matrix @ p_up > {LONG_THRESHOLD}")
    print(f"      actually up     : TP={tp:5d}  FN={fn:5d}")
    print(f"      actually not-up : FP={fp:5d}  TN={tn:5d}")

    predicted_up = tp + fp
    if predicted_up:
        # Of the days we would have traded long, how many actually rose.
        print(f"      precision (of predicted-up): {tp / predicted_up:.4f}")
    else:
        print(f"      precision (of predicted-up): n/a (never crossed {LONG_THRESHOLD})")


def print_calibration(y_true: np.ndarray, p_up: np.ndarray) -> None:
    """Compare predicted probability against realized frequency, per bucket.

    A calibrated 0.60 should resolve up ~60% of the time. This matters beyond
    ranking because Phase D turns p_up into position size, so an overconfident
    probability becomes an oversized bet.
    """
    edges = np.arange(0.0, 1.0 + CALIBRATION_BIN_WIDTH, CALIBRATION_BIN_WIDTH)
    bucket = np.digitize(p_up, edges) - 1

    print("    calibration (predicted vs realized)")
    for b in range(len(edges) - 1):
        mask = bucket == b
        count = int(mask.sum())
        if count == 0:
            continue
        print(
            f"      [{edges[b]:.2f}, {edges[b + 1]:.2f})  "
            f"n={count:5d}  predicted={p_up[mask].mean():.4f}  realized={y_true[mask].mean():.4f}"
        )


def print_naive_baseline(y_train: np.ndarray, y_true: np.ndarray, p_up: np.ndarray) -> None:
    """Compare against a model that knows nothing.

    If most days are "up", always predicting up scores well for free. Accuracy
    only means something relative to that bar, not relative to 50%.
    """
    majority_class = int(round(y_train.mean()))
    naive_accuracy = (y_true == majority_class).mean()
    model_accuracy = ((p_up > LONG_THRESHOLD).astype(int) == y_true).mean()

    print("    naive baseline")
    print(f"      always predict {majority_class} (train majority): accuracy={naive_accuracy:.4f}")
    print(f"      model @ {LONG_THRESHOLD}                        : accuracy={model_accuracy:.4f}")
    print(f"      test base rate (fraction up)          : {y_true.mean():.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward validation of the baseline logistic regression models."
    )
    parser.add_argument(
        "--n-folds",
        type=int,
        default=N_FOLDS,
        help=f"Number of walk-forward folds (default {N_FOLDS}).",
    )
    parser.add_argument(
        "--embargo-days",
        type=int,
        default=EMBARGO_DAYS,
        help=f"Trading days skipped after each cutoff to purge overlapping "
        f"feature windows (default {EMBARGO_DAYS}).",
    )
    parser.add_argument(
        "--initial-train-fraction",
        type=float,
        default=INITIAL_TRAIN_FRACTION,
        help=f"Fraction of the calendar reserved as the first fold's training "
        f"set (default {INITIAL_TRAIN_FRACTION}).",
    )
    args = parser.parse_args()

    # No host/user/password: same local peer-auth connection every other part
    # of this pipeline relies on (bare `psql -d edmp_engine`).
    with psycopg.connect(dbname=DBNAME) as conn:
        df = load_training_data(conn)
        print(f"Loaded {len(df)} rows from analytics.v_training_dataset")

        # Fold boundaries come from the trading calendar, not row positions, so
        # every asset shares the same cutoffs.
        unique_dates = sorted(df["trading_date"].unique())
        folds = generate_folds(
            unique_dates,
            n_folds=args.n_folds,
            initial_train_fraction=args.initial_train_fraction,
            embargo_days=args.embargo_days,
        )
        print(
            f"{len(folds)} folds over {len(unique_dates)} trading dates "
            f"(embargo {args.embargo_days} days)\n"
        )

        git_commit = get_git_commit()
        auc_up_by_fold: list[float] = []
        auc_large_by_fold: list[float] = []

        # All folds commit together, so a crash partway through never leaves a
        # half-recorded walk-forward run in the database.
        with conn.transaction():
            for fold in folds:
                train_df = df[df["trading_date"] <= fold.train_end]
                test_df = df[
                    (df["trading_date"] >= fold.test_start) & (df["trading_date"] <= fold.test_end)
                ]

                if train_df.empty or test_df.empty:
                    print(f"Fold {fold.index}: empty split, skipping")
                    continue

                print(
                    f"Fold {fold.index}: "
                    f"train {fold.train_start}..{fold.train_end} ({len(train_df)} rows)  "
                    f"test {fold.test_start}..{fold.test_end} ({len(test_df)} rows)"
                )

                # Refit the scaler inside each fold, on that fold's training
                # rows only. Fitting once outside the loop would leak later
                # folds' statistics into earlier ones.
                scaler = StandardScaler().fit(train_df[FEATURE_COLUMNS])
                X_train = scaler.transform(train_df[FEATURE_COLUMNS])
                X_test = scaler.transform(test_df[FEATURE_COLUMNS])

                y_train_up = train_df[TARGET_UP].astype(int).to_numpy()
                y_train_large = train_df[TARGET_LARGE].astype(int).to_numpy()
                y_test_up = test_df[TARGET_UP].astype(int).to_numpy()
                y_test_large = test_df[TARGET_LARGE].astype(int).to_numpy()

                # Two independent models sharing the same scaled features. Both are
                # required, not optional: model_predictions.p_large_move is NOT NULL,
                # so every row this script writes needs a real value for it.
                clf_up = LogisticRegression(max_iter=1000).fit(X_train, y_train_up)
                clf_large = LogisticRegression(max_iter=1000).fit(X_train, y_train_large)

                p_up = predict_proba_class1(clf_up, X_test)
                p_large_move = predict_proba_class1(clf_large, X_test)

                signal = np.where(
                    p_up > LONG_THRESHOLD, 1, np.where(p_up < SHORT_THRESHOLD, -1, 0)
                ).astype(int)

                auc_up = safe_auc(y_test_up, p_up)
                auc_large = safe_auc(y_test_large, p_large_move)
                if auc_up is not None:
                    auc_up_by_fold.append(auc_up)
                if auc_large is not None:
                    auc_large_by_fold.append(auc_large)

                print(
                    f"    ROC-AUC  y_up_next_day={'n/a' if auc_up is None else f'{auc_up:.4f}'}  "
                    f"y_large_move_next={'n/a' if auc_large is None else f'{auc_large:.4f}'}"
                )
                print_confusion_matrix(y_test_up, p_up)
                print_calibration(y_test_up, p_up)
                print_naive_baseline(y_train_up, y_test_up, p_up)

                with conn.cursor() as cur:
                    # Each fold is its own model_runs row. The schema already
                    # distinguishes them by train/test date ranges, so no new
                    # column is needed to record the walk-forward structure.
                    cur.execute(
                        """
                        INSERT INTO analytics.model_runs
                            (model_name, feature_set_version, train_start, train_end,
                             test_start, test_end, git_commit)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING model_run_id
                        """,
                        (
                            MODEL_NAME,
                            FEATURE_SET_VERSION,
                            fold.train_start,
                            fold.train_end,
                            fold.test_start,
                            fold.test_end,
                            git_commit,
                        ),
                    )
                    model_run_id = cur.fetchone()[0]

                    # Test-split rows only, never train. model_predictions has
                    # no train/test marker column, so writing in-sample rows
                    # here would silently corrupt any later backtest that reads
                    # this table expecting genuine out-of-sample forecasts.
                    rows = list(
                        zip(
                            [model_run_id] * len(test_df),
                            test_df["asset_id"].tolist(),
                            test_df["trading_date"].tolist(),
                            p_up.tolist(),
                            p_large_move.tolist(),
                            signal.tolist(),
                        )
                    )
                    cur.executemany(
                        """
                        INSERT INTO analytics.model_predictions
                            (model_run_id, asset_id, trading_date, p_up, p_large_move, signal)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        rows,
                    )

                print(f"    model_run_id={model_run_id}: wrote {len(rows)} predictions\n")

        # The spread across folds is the actual result. A tight band means a
        # stable edge; a wide one means the single-split number was luck.
        if auc_up_by_fold:
            arr = np.array(auc_up_by_fold)
            print(
                f"y_up_next_day     ROC-AUC over {len(arr)} folds: "
                f"mean={arr.mean():.4f}  std={arr.std():.4f}  min={arr.min():.4f}  max={arr.max():.4f}"
            )
        if auc_large_by_fold:
            arr = np.array(auc_large_by_fold)
            print(
                f"y_large_move_next ROC-AUC over {len(arr)} folds: "
                f"mean={arr.mean():.4f}  std={arr.std():.4f}  min={arr.min():.4f}  max={arr.max():.4f}"
            )


if __name__ == "__main__":
    main()
