# Design Decisions & Engineering Rationale

This document explains **why** the project is built the way it is — the reasoning behind the
architecture, the modelling choices, the validation discipline, and the tests. The README
covers *what* the system does and how to run it; this covers the decisions underneath, including
the ones that were deliberately *not* taken and the problems that remain open.

It is written to be readable by someone who has never seen the codebase.

---

## 1. What this project is, and what it is not

EDMP Engine estimates **P(market move | features)**. It is a probability engine, not a
prediction engine, and that distinction drives most of what follows.

Markets are stochastic. A model that observes six price-derived features is observing a tiny
subset of the information that actually moves prices. Claiming "the market will go up tomorrow"
would be overclaiming; producing "0.54, and here is how well-calibrated that number is" is
honest and actually usable — probabilities support position sizing, expected-value calculations,
and risk management in a way a binary call does not.

The second design commitment: **the pipeline is judged by whether its conclusions are
trustworthy, not by whether they are impressive.** A validated "these features do not predict
next-day direction" is a genuine result. An unvalidated ROC-AUC of 0.65 is not a better result;
it is an unexamined one. Nearly every decision below trades away potential upside for the
ability to trust the output.

---

## 2. Warehouse architecture

### Three schemas, one direction of flow

```
raw.*        as-ingested CSV data, minimal constraints
  ↓
staging.*    cleaned, typed, symbol → asset_id resolved
  ↓
analytics.*  features, labels, model outputs
```

**Why separate `raw` at all**, rather than cleaning during ingestion? Because the original
source data is the one thing that cannot be regenerated if a transformation turns out to be
wrong. Keeping `raw` untouched means every transformation downstream can be re-run, corrected,
and re-run again without another network fetch. The cost is disk space; the benefit is that no
mistake in the transform layer is unrecoverable.

**Why `staging` between raw and analytics?** It is where the surrogate key appears. `raw.*`
tables key on natural identifiers (`symbol`, `symbol + trading_date`) because that is what the
CSV contains. From `staging` onward everything re-keys on `asset_id`, so a ticker rename or a
symbol collision cannot silently corrupt joins downstream.

### Truncate-and-recompute, not incremental update

Every numbered SQL script wraps its work in `BEGIN;`/`COMMIT;` and `TRUNCATE`s its own output
before recomputing. `make run` rebuilds the entire warehouse from scratch.

This is slower than incremental updating and it is the right trade here. Incremental pipelines
accumulate **hidden state**: a row computed under last month's feature definition sits
indistinguishably beside rows computed under this month's. In a system whose whole premise is
that the numbers can be trusted, "I am not sure which version of the logic produced this row" is
disqualifying. A full rebuild is deterministic — the same inputs always produce the same
warehouse.

`20_staging_transform.sql` truncates all of `staging.*` **and** `analytics.*` before rebuilding,
because downstream analytics would otherwise be orphaned — pointing at `asset_id` values that
staging no longer defines.

### The one deliberate exception: model tables are append-only

`analytics.model_runs` and `analytics.model_predictions` are **never** truncated. This
contradicts the pattern above, and the contradiction is intentional:

- The SQL stages recompute **deterministic derived state**. Rebuilding must reproduce it exactly,
  so truncate-and-recompute is correct.
- `model_runs` is an **experiment log** — `bigserial` primary key, `created_at`, `git_commit`.
  Its entire purpose is to let runs from different code versions, feature sets, and date cutoffs
  coexist so they can be compared. Truncating it would destroy the history it exists to record.

Because `model_run_id` is freshly allocated on every insert and forms part of
`model_predictions`'s composite primary key, reruns can never collide. No `ON CONFLICT` logic is
needed, and none should be added.

A consequence worth noting: `make train_baseline` is deliberately **not** part of `make run`.
`make run` is documented as the deterministic rebuild; folding in a step that appends a new row
on every invocation would break that guarantee on a routine data refresh.

---

## 3. Point-in-time correctness — the central constraint

This is the one rule the entire project is organised around:

> A feature at time `t` may only use information available at or before `t`.
> A label at time `t` describes the outcome at `t+1`.

Formally: `X_t = f({data_τ : τ ≤ t})` and `y_t = outcome_{t+1}`.

Violating this is called **lookahead bias** or **leakage**, and it is uniquely dangerous because
it is *silent*. Nothing crashes. No row count changes. The model simply performs better than it
should, because it is being shown answers. Every downstream number — ROC-AUC, calibration,
Sharpe ratio — becomes a lie, and nothing in the output indicates that.

### How the design enforces it structurally

**Separate tables, not a convention.** Forward-looking values live exclusively in
`analytics.labels_daily`. Features live in `analytics.features_daily`. This is not stylistic:
it means a leak requires someone to physically put a `LEAD()` in the features file, which is
visible in review, rather than to subtly mis-order an expression inside a shared query.

**Trailing-only window frames.** Every feature uses
`ROWS BETWEEN N PRECEDING AND CURRENT ROW`. The phrase `CURRENT ROW` is what guarantees the
window stops at `t`.

**Partitioned by asset.** Every window carries `PARTITION BY asset_id ORDER BY trading_date`, so
one asset's history cannot bleed into another's. Without this, a shorter-history asset would
inherit rows from whichever asset preceded it.

**A filtered join as the single entry point.** `analytics.v_training_dataset` joins features to
labels and drops any row with a NULL in either. The model reads from this view and nothing else,
so there is exactly one place where the training set is defined.

---

## 4. Feature design

Six features, each capturing a different aspect of market state:

| Feature | Captures | Why it earns its place |
| --- | --- | --- |
| `ret_1d` | the immediate move | the most basic state variable |
| `logret_1d` | the same, additively | log returns sum across time; simple returns do not, which matters for any multi-period aggregation |
| `vol_20d` | regime uncertainty | ~1 trading month of realised volatility. Models behave differently in calm vs turbulent regimes |
| `mom_5d` | short-term trend | short-horizon continuation is among the more robust empirical anomalies |
| `mom_20d` | medium-term trend | paired with `mom_5d`, describes multi-horizon trend structure rather than a single timescale |
| `drawdown_60d` | stress / risk state | distance below the trailing 60-day peak. Proximity to a crash is information that a return series alone does not convey |

Together these encode short-term state, medium-term state, risk regime, and directional bias —
without requiring deep learning to extract. The deliberate choice is **interpretability over
capacity**: logistic regression coefficients on these six are inspectable, and at this stage
knowing *why* a model does nothing is more valuable than having a black box that does slightly
more.

---

## 5. Label design

Two labels, both in `labels_daily`:

**`y_up_next_day`** = `ret_fwd_1d > 0`. Direction.

**`y_large_move_next`** = `ABS(ret_fwd_1d) > 2.0 * vol_20d`. Magnitude.

The magnitude label's threshold is **volatility-scaled per asset**, not a fixed percentage. This
matters more than it first appears. A fixed threshold like "2% move" would make TLT (a bond ETF)
almost never register a large move and a volatile equity register them constantly — the label's
base rate would be an artifact of which asset it is, and a pooled model would learn asset
identity rather than anything about market state. Scaling by each asset's own trailing
volatility keeps base rates comparable across bonds, gold, and equities, which is what makes
pooling a heterogeneous universe defensible at all.

**Why separate labels from features** rather than computing both in one place:

1. Label definitions change more often than feature definitions. Separation means redefining
   "large move" does not touch feature logic.
2. It makes the leakage rule enforceable by inspection — the forward-looking code is all in one
   file.

---

## 6. How validation evolved, and why

### Phase A: a single chronological split

The first working model trained on the earliest 80% of dates and tested on the last 20%.
Chronological, never random — a random split would let the model train on data from *after* what
it is predicting.

Result: ROC-AUC **0.4934** for direction, **0.6031** for large moves.

### Why one split is not enough

**One split is one sample.** Markets have regimes. A model tested on a calm 2019 window and a
model tested on a volatile 2022 window can produce very different numbers from identical code,
and a single split gives no way to know which one you drew.

There is also a subtler problem specific to these features. `vol_20d`, `mom_20d` and
`drawdown_60d` are rolling windows: the row for a given date depends on the 20 or 60 days before
it. Rows near the train/test boundary therefore have features computed from a **mix** of
train-side and test-side prices. This is not full label leakage, but it means adjacent rows are
not independent — which is precisely the assumption that random k-fold cross-validation relies
on. Hence the rule: never random.

### Phase B: walk-forward validation

Five expanding-window folds. Each fold trains on all history up to its cutoff, tests on the
block after it, and the next fold's cutoff is the previous fold's test-block end — so the
training set grows monotonically.

The output is not a number but a **distribution**. A mean of 0.55 across folds ranging 0.48–0.62
tells a very different story from a mean of 0.55 where every fold sits at 0.54–0.56. The spread
*is* the result.

Expanding window was chosen over rolling window because the dataset is short (~2,100 trading
dates). A rolling window would discard early history that the model can still learn from. If the
universe grows and old regimes start looking irrelevant, rolling becomes the better choice.

### The embargo

To close the rolling-window leak described above, the first **60 trading days** after each
cutoff are dropped from the test block. Sixty matches `drawdown_60d`, the longest lookback in the
feature set — so every evaluated test row has its entire feature window on the test side of the
boundary.

This costs test data at every fold boundary. That is the correct trade: fewer rows evaluated
honestly beats more rows evaluated with a known contaminant.

**This constraint is now mechanically enforced.** `test_embargo_covers_the_longest_feature_lookback`
parses the lookback out of each feature name (`mom_20d` → 20, `drawdown_60d` → 60) and asserts
`EMBARGO_DAYS >= max`. Adding a longer feature such as `mom_120d` without raising the embargo
fails the test suite instead of silently reintroducing the overlap.

---

## 7. Evaluating honestly

Once real out-of-sample predictions exist, the question stops being "does it work" and becomes
"how well, and is that believable". Three checks, each answering something the others cannot.

### ROC-AUC

The probability that a randomly chosen positive day scores higher than a randomly chosen negative
day, across all thresholds at once. `0.5` is a coin flip; `1.0` is perfect separation.

It is threshold-independent, which is the point — it measures the quality of the probability
*ranking* itself, separately from whatever cutoff is later chosen for trading.

**Why a value near 0.5 is the expected outcome, not a failure.** Daily equity direction is close
to a random walk. A realistic, honest edge from price-derived features lands around **0.52–0.56**.
Small edges are what real quantitative signals look like; they compound across many trades rather
than winning big on any one. The suspicious direction is the *opposite* one — a simple logistic
regression on six basic features scoring 0.65+ is far more likely to indicate leakage or a lucky
split than genuine alpha. That expectation is written into the leakage-audit procedure as a
trigger for investigation.

### Confusion matrix at the trading threshold

ROC-AUC summarises ranking across every threshold. The confusion matrix answers what happens at
the **one threshold actually used** (`p_up > 0.55`). A model can post a decent AUC and still be
useless in practice if all its separation occurs at thresholds nobody would trade on.

### Calibration

A stated probability of 0.60 should resolve upward about 60% of the time. Predictions are
bucketed by `p_up` and compared against realised frequency.

This matters beyond ranking because the planned backtest turns `p_up` into **position size**
(`p_up - 0.5`). If 0.60 actually resolves up 51% of the time, every sizing decision is wrong even
though the ranking looked fine.

### Comparison against a naive baseline

Before trusting any accuracy figure, compare against a model that knows nothing: always predict
the majority class. If 53% of days are up, a naive model gets 53% accuracy for free. Beating 0.5
and beating the naive baseline are **different bars**, and conflating them is one of the easiest
ways to fool yourself.

---

## 8. What the results actually say

Walk-forward, 5 folds, 3 ETFs (SPY, TLT, XLE), 2018 to present, 6,438 training rows:

| Target | Mean ROC-AUC | Std | Range |
| --- | --- | --- | --- |
| `y_up_next_day` | 0.4934 | 0.0279 | 0.4595 – 0.5333 |
| `y_large_move_next` | 0.5928 | 0.0636 | 0.5177 – 0.6846 |

**Direction: no signal.** The distribution straddles 0.5 across every fold. These six
price-derived features do not predict next-day direction. This is the expected result in a
liquid, efficient market — and it confirms Phase A's single-split figure was not an unlucky draw.

**Large moves: a real but modest edge.** Every fold clears 0.5, mean 0.59. This makes mechanical
sense: `vol_20d` and `drawdown_60d` are literally volatility and stress measures, so "will
tomorrow be a large move" is a far more natural question for this feature set than "which
direction". The 0.52–0.68 spread is wide, though, which is exactly the kind of instability a
single split would have hidden.

Two findings surfaced **only** because of the honest-evaluation layer:

**The naive baseline beats the model on accuracy in every single fold** (fold 3: 0.5844 naive vs
0.4156 model). The model rarely clears the 0.55 threshold — single-digit true positives per fold
— so it mostly abstains while ~53% of days rise anyway. Clearing 0.5 AUC while losing to the
naive baseline on accuracy is not a contradiction; it is why both checks are needed.

**Calibration is poor exactly where it would trade.** The 0.55–0.60 bucket resolves upward
roughly 33–45% of the time instead of ~57%. The model is overconfident precisely in the range
that would trigger a position. This is a hard blocker on proportional position sizing in the
planned backtest — sizing off these probabilities would systematically oversize bad bets — and it
is recorded as a prerequisite rather than discovered later.

---

## 9. Testing strategy

### The problem tests are solving here

In most software a bug announces itself: an exception, a wrong value, a failed request. In a
data pipeline, a temporal-leakage bug produces a *plausible* number that happens to be wrong.
Nothing surfaces it. Tests are therefore not about catching crashes — they are the only mechanism
by which a silent correctness failure becomes visible.

### Two layers, guarding opposite ends

| File | Guards | Cost |
| --- | --- | --- |
| `sql/10_validations.sql` | raw **input**: nulls, negative prices, `high < low`, duplicate keys, future dates | runs inside `make run`, before staging |
| `sql/90_assertions.sql` | computed **output**: re-derives each value from its definition | runs in `make test`, needs a built warehouse |
| `tests/test_folds.py` | fold construction logic | pytest, no database, milliseconds |

The split by cost is deliberate: pytest runs on every edit, the SQL assertions run after a
pipeline rebuild.

### The six assertions in `sql/90_assertions.sql`

**1. `ret_fwd_1d(t) == ret_1d(t+1)`** — the strongest invariant available. Both sides equal
`close(t+1)/close(t) - 1` by definition: the label reaches forward from `t`, the feature looks
back from `t+1`, and they must describe the same move. Any off-by-one shift in a `LEAD`/`LAG`, or
a window frame that lost its `PARTITION BY asset_id`, makes them diverge. This single check
catches the exact class of bug that would otherwise inflate every metric downstream.

**2. Label definitions** — `y_up_next_day == (ret_fwd_1d > 0)` and
`y_large_move_next == (ABS(ret_fwd_1d) > 2.0 * vol_20d)`. Stops a redefinition landing silently
while the documentation still describes the old rule.

**3. Sign sanity** — `vol_20d >= 0` (it is a standard deviation) and `drawdown_60d <= 0` (the
current row is inside its own trailing-max window, so the ratio cannot exceed 1). A violation
means the window frame or the arithmetic is wrong, not that the market did something unusual.

**4. No NULLs in `v_training_dataset`** — the view already filters these, so this asserts the
filter still covers every column it selects. Adding a column to the `SELECT` without adding it to
the `WHERE` is an easy mistake that would hand NaNs to scikit-learn.

**5. Warm-up exclusion** — per-asset row count must equal (price rows − 21). `mom_20d` needs 21
rows so the first 20 are unusable, and `LEAD` leaves the final row without a forward return. This
also guards a subtler failure: if a window frame lost its partitioning, later assets would
inherit warm-up from earlier ones and gain rows they should not have.

**6. Predictions are strictly out-of-sample** — every `model_predictions` row must fall inside
its own run's `test_start..test_end`, and none may have `trading_date <= train_end`. The training
script writes only test-split rows, but that is a convention enforced in Python, and the table has
no train/test marker column to prevent otherwise. This asserts the rule **at the database level**,
where it cannot be bypassed. Without it, in-sample fitted values could be read by a later backtest
as genuine forecasts, reporting an edge that does not exist.

### Proving the tests can fail

**A test suite that has never failed is unverified.** An assertion with a subtly wrong predicate
passes unconditionally and is *worse* than no assertion, because it manufactures confidence.

So each invariant was confirmed by deliberately breaking it. For the key assertion: a single
`ret_fwd_1d` value in `analytics.labels_daily` was corrupted to `0.999`, the suite was re-run, and
it failed with

```
ERROR: Assertion failed: ret_fwd_1d(t) <> ret_1d(t+1) on 1 row(s), max diff 0.9926748436338823.
Features and labels disagree about which day is "next" — suspect a shifted LEAD/LAG or a window
frame that is not partitioned by asset_id.
```

exiting non-zero. The labels table was then rebuilt from `40_analytics_labels.sql` and the suite
confirmed green again. The failure message names the likely cause rather than merely reporting a
mismatch, because an assertion that fires without pointing anywhere costs debugging time it should
be saving.

### The suite passes on the *current* pipeline — and that timing was the point

All six assertions were written and confirmed passing **before** any restructuring work began.
Critically, `ret_fwd_1d(t) == ret_1d(t+1)` passes today, which establishes there is **no
pre-existing shift bug**.

That ordering is not incidental. Had the assertions been written after the upcoming `adj_close`
migration, a failure would have been ambiguous: did the migration break it, or was it always
broken? Building against known-good data first makes every future failure attributable to the
change that produced it.

### Ratchet and frontier

Automated tests catch the invariants someone has **already written down**. They cannot catch a
*novel* mistake — a newly added feature using `LEAD` trips no existing assertion, and a scaler fit
outside the fold loop leaves no trace in the database at all.

That gap is covered by an on-demand audit procedure (`.claude/skills/leakage-audit/`) encoding
this project's specific rules and known subtleties.

The two feed each other:

- **Ratchet** (`make test`) — mechanical, never skipped, never forgotten.
- **Frontier** (audit) — judgement, applied to changes no assertion covers yet.

When the audit finds something, it becomes a new assertion, so the ratchet absorbs that class
permanently. `test_embargo_covers_the_longest_feature_lookback` is the first example: it began as
a judgement call ("did anyone raise `EMBARGO_DAYS` after adding a longer window?") and is now a
test that parses feature names and fails on its own.

A `PostToolUse` hook (`.claude/hooks/comment_reminder.sh`) completes the loop by prompting for the
*reasoning* behind edits to `.py`/`.sql` files. In a pipeline like this a wrong comment is cheap;
a wrong assumption about what a window frame may see at time `t` is expensive. The hook is
strictly advisory and always exits zero — a hook able to break the edit loop over a style concern
would be a worse trade than an occasional missing comment.

---

## 10. Known limitations and open issues

Stated plainly, because a limitations section that reads like marketing is worthless.

**Features are computed from unadjusted `close`.** `sql/30_analytics_features.sql` and
`sql/40_analytics_labels.sql` use `close`, never `adj_close`. With the current ETF universe this
is nearly harmless — only dividend adjustments differ. It becomes a **serious bug** the moment
individual stocks are added: AAPL's 4:1 split (2020), AMZN and GOOGL's 20:1 (2022) and NVDA's
10:1 (2024) would each inject a spurious −75% to −95% single-day return, poisoning `vol_20d` for
20 days and `drawdown_60d` for 60, and manufacturing false labels. This is the next scheduled
fix and it blocks the universe expansion.

*A subtlety that fix must respect*: adjusted prices are **retroactively restated** — a split
rescales all prior `adj_close` values, so today's series is not what a real-time observer saw.
This is safe here **only because every feature is a ratio**, in which a uniform rescaling cancels.
The argument breaks the moment any feature uses a raw price *level*.

**The universe is 3 ETFs.** 6,438 rows sounds substantial but is 2,146 dates × 3 correlated
instruments. Every result rests on very little independent information.

**Row count will always overstate independent information.** Assets trade on the same days and
share market beta, so N assets on one date are nowhere near N independent observations — the
effective sample size is far closer to the number of trading dates. Expanding the universe should
be expected to tighten the *variance* of estimates more than to move their *means*.

**Events are not implemented.** `raw.events` and `staging.events` exist in the schema, but
`prepare_data.py` writes an empty (schema-valid) `events.csv`. `staging.event_asset_map` is
populated by a `CROSS JOIN` mapping every event to every asset at weight 1.0 — a placeholder
explicitly labelled as such in the file, not a mapping rule. The project is named for
event-driven analysis and does not yet do any.

**No backtest exists.** `analytics.backtest_results` is defined and unused. Until it exists, none
of the results above have been translated into anything resembling a return series, and no
transaction-cost or slippage assumption has been tested.

**Probabilities are not calibrated.** Documented above; blocks proportional position sizing.

---

## 11. Decisions deliberately deferred

Things consciously *not* done, with the reasoning, so they read as choices rather than oversights.

**Not tuning the direction model.** A 0.49 ROC-AUC across five properly embargoed folds is not a
hyperparameter problem. It is the finding that these features do not predict direction. Iterating
against the same five folds until something clears 0.55 would be fitting the validation set — the
multiple-comparisons trap — and the resulting number would mean nothing.

**Not reaching for gradient-boosted trees.** More model capacity does not manufacture signal that
is not in the features. XGBoost becomes appropriate when event-interaction terms exceed what a
linear model can express, not before, and it costs the coefficient interpretability that currently
makes the model's behaviour inspectable.

**Not calibrating the direction model.** Calibration fixes how probabilities are *stated*; it
cannot create signal. A perfectly calibrated 0.49-AUC model correctly reports that it knows
nothing. Calibrating the large-move model is worthwhile, since it has an actual edge.

**Not building per-sector models yet.** When the universe expands, evaluation will be sliced by
sector while training stays pooled. Separate per-sector models would split the data roughly
elevenfold and multiply the multiple-comparisons problem, for insight that slicing a pooled
model's evaluation already provides.

**Not adding an ORM or a config framework.** Database access is raw `psycopg` with
`connect(dbname="edmp_engine")` — no host, user, or password — matching the bare
`psql -d edmp_engine` convention used everywhere else. A pipeline this size does not need a
configuration layer, and adding one would create a second place where connection behaviour is
defined.

**Not implementing events before the baseline was validated.** Events are the project's headline
concept, and building them first was the tempting order. But without a working baseline there
would be no way to measure whether event features added anything — the point of a baseline is to
be the thing the next increment is measured against. Event ingestion (alignment, mapping, missing
data, time zones) is also the highest-uncertainty work in the roadmap; doing it before the
evaluation machinery existed would have meant debugging two hard problems at once.

---

## 12. Reading order for the rest of the documentation

- **`README.md`** — what the system does, how to run it, the phase roadmap and its status.
- **`docs/course_validation_and_backtesting.md`** — the concepts behind walk-forward validation
  and backtesting, written against this project's actual tables.
- **`CLAUDE.md`** — conventions and constraints for anyone (human or agent) modifying the code.
- **`docs/architecture/`** — the warehouse schema diagram.

---

## 13. Chronological build log

The phase roadmap in README.md describes the plan; this is the order it was actually built in,
from `git log`, for anyone trying to see how one decision led to the next.

| Date | Commit | What landed |
| --- | --- | --- |
| 2026-03-03 | `d9e2166` | Initial commit: `raw`/`staging`/`analytics` warehouse skeleton, price ingestion, first features + labels. |
| 2026-03-04 | `e9e93a5`, `718f4bc` | README iteration. |
| 2026-03-27 | `2703a94` | `prepare_data.py` added — CSV directory creation was a missing reproducibility step. |
| 2026-03-27 | `c7a63fa` | Makefile fixed (tab indentation) and `validate` stage added — raw-input checks now run before staging. |
| 2026-03-27 | `adab5fa`, `ecc5461` | Makefile/README follow-up. |
| 2026-03-28 | `7fce22d` | `ingest_yahoo_daily.py` merged into `prepare_data.py` — one ingestion entry point instead of two. |
| 2026-03-28 | `fc80320` | `analytics.v_training_dataset` added and wired into `make run` — the single join point features/labels feed the model from. |
| 2026-08-22 | `2895d5d` | **Phase A**: baseline logistic regression, single chronological 80/20 split. First result: ROC-AUC 0.4934 (direction), 0.6031 (large moves). |
| 2026-08-22 | `2125ab8` | **Phases B & C**: walk-forward validation (5 expanding-window folds) replaces the single split; embargo, calibration buckets, and naive-baseline comparison added. Confirmed Phase A's numbers weren't a one-off draw. |
| 2026-08-22 | `7411533` | Leakage-safety infrastructure — `sql/90_assertions.sql`, `tests/test_folds.py`, the `leakage-audit` skill — added ahead of the (then-upcoming) universe restructure, deliberately before any risky change so a later failure is attributable to that change and not a pre-existing bug. |
| 2026-08-23 | `fbf9bc9` | `docs/design_decisions.md` added, capturing the reasoning behind everything above. |
| 2026-08-24 | *(uncommitted)* | Features and labels switched from `close` to `adj_close` — the fix for the "unadjusted close" limitation flagged in section 10, verified safe by the leakage-audit skill and a clean `sql/90_assertions.sql` run. Precedes the universe expansion that would otherwise have made stock splits corrupt `vol_20d`/`drawdown_60d`. |

The gap between March and August is not a documented decision — just time away from the project.
Everything from Phase A onward happened in one concentrated stretch, which is why those entries
read as a single coherent arc while the March commits are more piecemeal setup.
