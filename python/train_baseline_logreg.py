from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DBNAME = "edmp_engine"

TRAIN_FRACTION = 0.8
FEATURE_COLUMNS = ["ret_1d", "logret_1d", "vol_20d", "mom_5d", "mom_20d", "drawdown_60d"]
TARGET_UP = "y_up_next_day"
TARGET_LARGE = "y_large_move_next"

MODEL_NAME = "logreg_baseline"
FEATURE_SET_VERSION = "v1"

LONG_THRESHOLD = 0.55
SHORT_THRESHOLD = 0.45

def load_training_data(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM analytics.v_training_dataset ORDER BY trading_date, asset_id",
        conn,
    )


def choose_train_end(df: pd.DataFrame, train_end_arg: str | None) -> date:
    if train_end_arg is not None:
        return date.fromisoformat(train_end_arg)

    unique_dates = sorted(df["trading_date"].unique())
    cutoff_idx = int(len(unique_dates) * TRAIN_FRACTION)
    return unique_dates[cutoff_idx]


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
        return None


def predict_proba_class1(clf: LogisticRegression, X: np.ndarray) -> np.ndarray:
    assert clf.classes_.tolist() == [0, 1], f"unexpected classes_: {clf.classes_}"
    return clf.predict_proba(X)[:, 1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Train baseline logistic regression models and write predictions.")
    parser.add_argument(
        "--train-end",
        type=str,
        default=None,
        help="YYYY-MM-DD chronological train/test cutoff (inclusive of train). "
        f"Defaults to the date at {TRAIN_FRACTION:.0%} of the available trading dates.",
    )
    args = parser.parse_args()

    with psycopg.connect(dbname=DBNAME) as conn:
        df = load_training_data(conn)
        print(f"Loaded {len(df)} rows from analytics.v_training_dataset")

        train_end = choose_train_end(df, args.train_end)
        train_df = df[df["trading_date"] <= train_end]
        test_df = df[df["trading_date"] > train_end]

        if train_df.empty or test_df.empty:
            raise RuntimeError(
                f"train_end={train_end} produces an empty split "
                f"(train={len(train_df)} rows, test={len(test_df)} rows)"
            )

        train_start = train_df["trading_date"].min()
        test_start = test_df["trading_date"].min()
        test_end = test_df["trading_date"].max()

        print(
            f"Train: {train_start} .. {train_end} ({len(train_df)} rows)  "
            f"Test: {test_start} .. {test_end} ({len(test_df)} rows)"
        )

        scaler = StandardScaler().fit(train_df[FEATURE_COLUMNS])
        X_train = scaler.transform(train_df[FEATURE_COLUMNS])
        X_test = scaler.transform(test_df[FEATURE_COLUMNS])

        y_train_up = train_df[TARGET_UP].astype(int)
        y_train_large = train_df[TARGET_LARGE].astype(int)

        clf_up = LogisticRegression(max_iter=1000).fit(X_train, y_train_up)
        clf_large = LogisticRegression(max_iter=1000).fit(X_train, y_train_large)

        p_up = predict_proba_class1(clf_up, X_test)
        p_large_move = predict_proba_class1(clf_large, X_test)

        signal = np.where(p_up > LONG_THRESHOLD, 1, np.where(p_up < SHORT_THRESHOLD, -1, 0)).astype(int)

        y_test_up = test_df[TARGET_UP].astype(int)
        try:
            auc_up = roc_auc_score(y_test_up, p_up)
            print(f"Test ROC-AUC (y_up_next_day): {auc_up:.4f}")
        except ValueError as exc:
            print(f"Could not compute test ROC-AUC (y_up_next_day): {exc}")

        y_test_large = test_df[TARGET_LARGE].astype(int)
        try:
            auc_large = roc_auc_score(y_test_large, p_large_move)
            print(f"Test ROC-AUC (y_large_move_next): {auc_large:.4f}")
        except ValueError as exc:
            print(f"Could not compute test ROC-AUC (y_large_move_next): {exc}")

        git_commit = get_git_commit()

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analytics.model_runs
                        (model_name, feature_set_version, train_start, train_end,
                         test_start, test_end, git_commit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING model_run_id
                    """,
                    (MODEL_NAME, FEATURE_SET_VERSION, train_start, train_end, test_start, test_end, git_commit),
                )
                model_run_id = cur.fetchone()[0]

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

        print(f"model_run_id={model_run_id}: wrote {len(rows)} rows to analytics.model_predictions")


if __name__ == "__main__":
    main()
