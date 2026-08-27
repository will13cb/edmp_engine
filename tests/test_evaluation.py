"""Invariants for the per-symbol evaluation breakdown.

A pooled ROC-AUC answers "does this feature set rank days correctly" and hides
which instruments it holds for. That distinction matters twice: substantively,
because an edge carried by two assets is not the same finding as a broad one, and
diagnostically, because duplicated price series produce identical per-symbol
numbers -- the signature of the ingestion bug recorded in
docs/design_decisions.md §9, which every per-asset assertion passed.

Pure: no database, no network.
"""

from __future__ import annotations

import numpy as np

from train_baseline_logreg import per_symbol_aucs, safe_auc


def test_safe_auc_returns_none_for_a_single_class_block():
    """The guard has to survive scikit-learn changing how it signals this.

    Older versions raised ValueError; 1.9 returns nan with a warning. nan is not
    None, so it slips through an `is not None` check, joins the list of per-fold
    scores, and silently turns the reported mean and std into nan. Nothing fails,
    the headline metric just stops being a number. Not hypothetical for
    y_large_move_next, whose base rate near 6% makes an all-negative short test
    block plausible.
    """
    assert safe_auc(np.array([1, 1]), np.array([0.4, 0.6])) is None
    assert safe_auc(np.array([0, 0]), np.array([0.4, 0.6])) is None
    assert safe_auc(np.array([0, 1]), np.array([0.2, 0.8])) == 1.0


def test_auc_is_computed_per_symbol_not_pooled():
    """Each symbol is scored on its own rows only.

    AAA is ranked perfectly, BBB is ranked exactly backwards. Pooling them would
    average toward 0.5 and report neither.
    """
    symbols = np.array(["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "BBB"])
    y_true = np.array([0, 0, 1, 1, 0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.8, 0.9, 0.9, 0.8, 0.2, 0.1])

    aucs = per_symbol_aucs(symbols, y_true, p)

    assert aucs["AAA"] == 1.0
    assert aucs["BBB"] == 0.0


def test_single_class_symbol_is_omitted_not_scored_as_half():
    """AUC is undefined when a test block has one class. Reporting 0.5 would be a
    fabricated value that drags the mean toward "no signal" and hides that the
    symbol was never actually evaluated."""
    symbols = np.array(["AAA", "AAA", "FLAT", "FLAT"])
    y_true = np.array([0, 1, 1, 1])
    p = np.array([0.2, 0.8, 0.4, 0.6])

    aucs = per_symbol_aucs(symbols, y_true, p)

    assert "AAA" in aucs
    assert "FLAT" not in aucs


def test_duplicated_series_produce_identical_scores():
    """The diagnostic property this table exists to expose.

    When ingestion hands two symbols the same underlying data, their features,
    labels and therefore predictions coincide, so their AUCs come out equal.
    Genuinely different instruments do not do that. This is what makes a wall of
    near-identical rows a signal to investigate rather than a curiosity.
    """
    y = np.array([0, 1, 0, 1])
    p = np.array([0.3, 0.7, 0.4, 0.6])

    symbols = np.array(["REAL"] * 4 + ["COPY"] * 4)
    aucs = per_symbol_aucs(symbols, np.concatenate([y, y]), np.concatenate([p, p]))

    assert aucs["REAL"] == aucs["COPY"]


def test_symbols_are_scored_independently_of_each_other():
    """A symbol's score must not shift when an unrelated symbol is added, or the
    table would not be readable as per-instrument evidence."""
    symbols = np.array(["AAA", "AAA", "AAA", "AAA"])
    y_true = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.6, 0.9])
    alone = per_symbol_aucs(symbols, y_true, p)["AAA"]

    with_other = per_symbol_aucs(
        np.concatenate([symbols, np.array(["ZZZ"] * 4)]),
        np.concatenate([y_true, np.array([0, 1, 0, 1])]),
        np.concatenate([p, np.array([0.9, 0.1, 0.8, 0.2])]),
    )["AAA"]

    assert alone == with_other
