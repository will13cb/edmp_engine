---
name: leakage-audit
description: Audit pipeline changes for temporal data leakage in the EDMP warehouse. Use after modifying anything under sql/ or python/ that touches features, labels, window frames, the training view, the train/test split, or what gets written to model_predictions — and before committing such a change. Also use when a model result looks suspiciously good (ROC-AUC well above ~0.55 on daily direction), since that is usually leakage rather than signal.
---

# Leakage audit

Temporal leakage in this project is **silent**. Nothing crashes, no row count changes, and the
only symptom is a metric that quietly becomes a lie. Assume nothing is fine because it ran.

Your output must end in one of two forms:

- **A concrete leakage path** — name the file, the exact expression, and the mechanism by which
  information from after time `t` reaches a feature at time `t`.
- **"No leakage found"**, plus which of the checks below you actually performed.

Vague reassurance is a failure. If you did not check something, say so rather than implying
coverage you do not have.

## 1. Establish what changed

```bash
git diff                 # uncommitted
git diff HEAD~1          # last commit, if auditing a completed step
git status --short
```

If the diff spans a half-finished migration (e.g. features moved to `adj_close` but labels not
yet), say so and audit only what is coherent. Reporting contradictions that are artifacts of an
intermediate state trains people to ignore this audit.

## 2. Run the mechanical checks first

```bash
make test
```

`sql/90_assertions.sql` already enforces the strongest invariant —
`ret_fwd_1d(t) == ret_1d(t+1)` — plus label definitions and the rule that every
`model_predictions` row falls inside its run's test window. If `make test` fails, that is your
finding; report it and stop. If pytest is unavailable, run the SQL half directly:

```bash
psql -v ON_ERROR_STOP=1 -d edmp_engine -f sql/90_assertions.sql
```

Passing tests are necessary, not sufficient. They cannot see a *newly added* feature that reads
the future. Continue.

## 3. The project's rules

From CLAUDE.md, "Point-in-time correctness":

- `analytics.features_daily` may only use data at or before `trading_date`. Every window frame
  must be trailing (`ROWS BETWEEN N PRECEDING AND CURRENT ROW`) and must
  `PARTITION BY asset_id`.
- `analytics.labels_daily` is the **only** table allowed to look forward (`LEAD`).
- A new label belongs in the labels table, never mixed into features.
- `analytics.v_training_dataset` filters NULLs; a column added to its SELECT list must also be
  added to the NULL filter, or NaNs reach sklearn.

## 4. Check each of these explicitly

**Feature expressions** (`sql/30_analytics_features.sql`) — any `LEAD`, any window frame with
`FOLLOWING`, any frame missing `PARTITION BY asset_id`, any join that could pull a later row.

**Label expressions** (`sql/40_analytics_labels.sql`) — forward-looking is correct here, but
confirm the horizon still matches what `90_assertions.sql` asserts.

**Price basis** — features and labels must use the *same* column. Mixed `close`/`adj_close`
across the two files breaks `ret_fwd_1d(t) == ret_1d(t+1)` and is a real bug.

**Adjusted prices are retroactively restated.** A split or dividend rescales all prior
`adj_close` values, so today's series is not what a real-time observer saw. This is safe **only
because every feature is a ratio** (`adj_close[t]/adj_close[t-k]`,
`adj_close/MAX(adj_close)`), where a uniform rescaling cancels. Verify this still holds — the
moment a feature uses a raw price *level*, the argument breaks and it becomes lookahead.

**The split and the embargo** (`python/train_baseline_logreg.py`) — folds must be built from
dates, never shuffled; `EMBARGO_DAYS` must be **≥ the longest rolling lookback** in
`FEATURE_COLUMNS` (currently 60, matching `drawdown_60d`). If a longer window was added and the
embargo was not raised, test rows near each boundary have features computed partly from
training-period prices. Report it.

**Fitting scope** — scalers, encoders, and imputers must be fit on each fold's *training* rows
only. A `.fit()` on the full frame, or outside the fold loop, leaks test-set statistics.

Encoding a **static, known-in-advance attribute** (an asset's sector, its asset_type) across the
full frame is *not* leakage — that information is available at time `t` by definition. Anything
derived from prices is.

**What gets written** — only test-split rows may reach `model_predictions`. The table has no
train/test marker, so in-sample rows would be read by a later backtest as genuine forecasts.

## 5. Sanity-check the numbers

Daily equity direction is close to a random walk. A walk-forward ROC-AUC meaningfully above
~0.55 on `y_up_next_day` is more likely a bug than an edge — treat it as a lead, not a win, and
hunt for the mechanism. The recorded honest baseline is ~0.49 for direction and ~0.59 for
`y_large_move_next`.

```sql
-- Predictions must sit strictly inside their run's test window.
SELECT r.model_run_id, r.train_end, r.test_start, r.test_end,
       min(p.trading_date), max(p.trading_date), count(*)
FROM analytics.model_predictions p
JOIN analytics.model_runs r USING (model_run_id)
GROUP BY 1,2,3,4 ORDER BY 1;
```
