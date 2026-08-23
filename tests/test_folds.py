"""Invariants for walk-forward fold construction.

generate_folds is pure logic over an ordered list of trading dates, and it is
where a silent catastrophe would live: a fold whose train and test windows
overlap invalidates every metric downstream while raising no error and changing
no row count. These tests assert the properties that make walk-forward
validation meaningful, rather than pinning exact fold boundaries (which would
just re-encode the implementation and break on any benign retune).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import numpy as np
import pytest

from train_baseline_logreg import (
    EMBARGO_DAYS,
    FEATURE_COLUMNS,
    Fold,
    generate_folds,
    predict_proba_class1,
)


def make_dates(n: int) -> list[date]:
    """n distinct ordered dates.

    Calendar realism is irrelevant: generate_folds only indexes positionally into
    this list, so "trading days" are list positions. Using consecutive calendar
    days keeps the fixture obvious.
    """
    start = date(2018, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


# 2146 is roughly the real dataset (2018-01 to 2026-08, 3 assets sharing one
# calendar), so the default case exercises realistic index arithmetic.
DATES = make_dates(2146)
DEFAULT_FOLDS = generate_folds(
    DATES, n_folds=5, initial_train_fraction=0.5, embargo_days=EMBARGO_DAYS
)


def test_folds_are_generated():
    assert len(DEFAULT_FOLDS) == 5
    assert all(isinstance(f, Fold) for f in DEFAULT_FOLDS)


def test_train_never_overlaps_test():
    """The defining property. If this fails, every reported metric is invalid."""
    for fold in DEFAULT_FOLDS:
        assert fold.train_end < fold.test_start, f"fold {fold.index} overlaps"
        assert fold.test_start <= fold.test_end


def test_embargo_gap_is_respected():
    """Exactly EMBARGO_DAYS trading days must sit between train_end and test_start.

    This is the purge that stops a test row's trailing feature windows (vol_20d,
    mom_20d, drawdown_60d) from being computed partly out of training-period
    prices. Measured in list positions because the embargo counts trading days,
    not calendar days.
    """
    index_of = {d: i for i, d in enumerate(DATES)}
    for fold in DEFAULT_FOLDS:
        gap = index_of[fold.test_start] - index_of[fold.train_end]
        assert gap == EMBARGO_DAYS + 1, f"fold {fold.index} gap {gap}"


def test_window_actually_expands():
    """Each fold must train on strictly more history than the last."""
    train_ends = [f.train_end for f in DEFAULT_FOLDS]
    assert train_ends == sorted(train_ends)
    assert len(set(train_ends)) == len(train_ends)


def test_all_folds_share_a_start():
    """Expanding window, not rolling: every fold trains from the first date."""
    assert all(f.train_start == DATES[0] for f in DEFAULT_FOLDS)


def test_test_blocks_do_not_overlap():
    """Otherwise the same rows would be scored more than once, and the
    across-fold mean would silently double-count them."""
    ordered = sorted(DEFAULT_FOLDS, key=lambda f: f.test_start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.test_end < later.test_start


def test_last_fold_reaches_the_final_date():
    """The last fold absorbs the remainder of the integer division, so no dates
    are silently dropped off the end of the evaluation."""
    assert DEFAULT_FOLDS[-1].test_end == DATES[-1]


def test_fold_indices_are_sequential():
    assert [f.index for f in DEFAULT_FOLDS] == [1, 2, 3, 4, 5]


def test_raises_when_embargo_consumes_the_block():
    """A silently empty test set would be far worse than a loud failure: the run
    would report metrics over almost no data."""
    with pytest.raises(RuntimeError, match="embargo_days"):
        generate_folds(
            make_dates(300), n_folds=5, initial_train_fraction=0.5, embargo_days=60
        )


def test_larger_embargo_shrinks_test_blocks():
    """Sanity on the trade-off: purging more rows must cost test data, not
    silently borrow rows from somewhere else."""
    index_of = {d: i for i, d in enumerate(DATES)}

    def first_block_size(embargo: int) -> int:
        fold = generate_folds(
            DATES, n_folds=5, initial_train_fraction=0.5, embargo_days=embargo
        )[0]
        return index_of[fold.test_end] - index_of[fold.test_start]

    assert first_block_size(120) < first_block_size(60)


def test_embargo_covers_the_longest_feature_lookback():
    """The embargo must be at least as long as the longest rolling window.

    A test row within N days of the cutoff has its N-day trailing window
    computed partly from training-period prices. Purging EMBARGO_DAYS after
    each cutoff only removes that overlap if the embargo is at least as long as
    the longest lookback in the feature set.

    Lookbacks are read from the column names (mom_20d -> 20, drawdown_60d ->
    60) so that adding a longer feature, say mom_120d, fails here instead of
    silently reintroducing the overlap. Names without a _<n>d suffix (ret_1d's
    sibling logret_1d aside) contribute nothing and are skipped.
    """
    lookbacks = [
        int(m.group(1))
        for col in FEATURE_COLUMNS
        if (m := re.search(r"_(\d+)d$", col))
    ]
    longest = max(lookbacks)

    assert EMBARGO_DAYS >= longest, (
        f"EMBARGO_DAYS={EMBARGO_DAYS} is shorter than the longest feature "
        f"lookback ({longest}d). Raise it to match, or test rows near each "
        f"fold boundary keep leaking training-period prices."
    )


class StubClassifier:
    """Minimal stand-in for a fitted LogisticRegression."""

    def __init__(self, classes):
        self.classes_ = np.array(classes)

    def predict_proba(self, X):
        # Column 0 = P(first class), column 1 = P(second class).
        return np.column_stack([np.full(len(X), 0.3), np.full(len(X), 0.7)])


def test_predict_proba_returns_positive_class_column():
    probs = predict_proba_class1(StubClassifier([0, 1]), np.zeros((4, 2)))
    assert np.allclose(probs, 0.7)


def test_predict_proba_rejects_flipped_class_order():
    """Guards a bug that would invert every probability while looking correct:
    with classes_ == [1, 0], column 1 is P(down), not P(up)."""
    with pytest.raises(AssertionError, match="unexpected classes_"):
        predict_proba_class1(StubClassifier([1, 0]), np.zeros((4, 2)))
