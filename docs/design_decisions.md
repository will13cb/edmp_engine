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
CSV contains. From `staging` onward everything re-keys on `asset_id`: one table,
`staging.assets`, defines what instruments exist, and every analytics table joins through it on a
narrow integer rather than repeating a text symbol.

It is worth being precise about what that does **not** buy, because surrogate keys usually imply
more than they deliver here. `asset_id` is regenerated on every rebuild, so it is not a durable
identity for an instrument — it is a within-generation join key. It offers no protection against
a ticker rename (renaming a symbol in `config/assets.csv` simply produces a different universe on
the next rebuild) and none against symbol collisions (`raw.assets.symbol` is already a primary
key, and `sql/10_validations.sql` rejects duplicates before staging is reached). Anything that
needs to refer to an instrument *across* rebuilds must use `symbol`, not `asset_id` — which is
exactly the constraint that shapes the model-table discussion below.

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

### The one deliberate exception: model tables are append-only within a generation

`analytics.model_runs` and `analytics.model_predictions` are never truncated **by the training
script**. Nothing there overwrites or upserts: every invocation adds rows.

- The SQL stages recompute **deterministic derived state**. Rebuilding must reproduce it exactly,
  so truncate-and-recompute is correct.
- `model_runs` is an **experiment log** — `bigserial` primary key, `created_at`, `git_commit`.
  Its purpose is to let runs from different code versions, feature sets, and date cutoffs coexist
  so they can be compared.

Because `model_run_id` is freshly allocated on every insert and forms part of
`model_predictions`'s composite primary key, reruns can never collide. No `ON CONFLICT` logic is
needed, and none should be added.

**But a warehouse rebuild does clear them, and must.** `sql/20_staging_transform.sql` truncates
`model_predictions`, `backtest_results`, `backtest_runs` and `model_runs` along with the rest, so `make run` empties
the experiment log. This is not an oversight in either direction, and it is worth being exact
about why, because the append-only claim above is easy to over-read.

`model_predictions.asset_id` is a foreign key into `staging.assets`, and staging is rebuilt with
`RESTART IDENTITY`, so `asset_id` values are reassigned from scratch on every run. Yesterday's
`asset_id = 7` and today's are not the same instrument — reordering `config/assets.csv` is enough
to shuffle them. Predictions that survived a rebuild would therefore be *silently misattributed*
rather than merely old: rows claiming a forecast for one asset that in fact describe another. The
choice is between losing the log and corrupting it, and losing it is plainly better.

So the accurate statement is that the log is append-only **within a warehouse generation** — it
accumulates across as many training runs as you like, and resets when the warehouse underneath it
is rebuilt. In practice that suits how the two commands are used: `make run` is the deterministic
rebuild, `make train_baseline` is the experiment, and runs are compared within a session rather
than across months.

If cross-rebuild history is ever needed, the cheap answer is `pg_dump` of the two tables before a
rebuild; the correct one is re-keying `model_predictions` on `symbol` rather than `asset_id`, so
it no longer depends on surrogate keys that are regenerated. That is deliberately not done yet —
see §11.

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

This is the headline metric everywhere in the project, so it is worth building up rather than
asserting.

**The problem it solves.** The model does not output a decision, it outputs a probability. To turn
that into "buy" or "don't", you pick a threshold — and every threshold trades two errors against
each other. Set it low and you catch nearly every up-day but also buy on many down-days. Set it
high and you are almost always right when you do buy, but you barely ever buy. Any single number
computed at one threshold (accuracy, precision) describes only that choice, and says nothing about
whether a *different* cutoff would have worked better.

**The ROC curve.** Sweep the threshold across every possible value and, at each one, plot two
quantities:

- **True positive rate** — of all the days that actually rose, what fraction did we flag?
- **False positive rate** — of all the days that did not rise, what fraction did we flag anyway?

At a threshold of 1.0 nothing is flagged, so both are 0. At 0.0 everything is flagged, so both are
1. In between, the curve traces the whole trade-off from one corner to the other. A model with no
information gains true positives at exactly the same rate it accumulates false ones: a straight
diagonal. A model with real information rises faster than it drifts right, bulging toward the
top-left corner.

**AUC is the area under that curve**, hence the name — 0.5 for the diagonal, 1.0 for a curve that
reaches the corner. And it has a second definition that is easier to reason about and provably
identical:

> Pick one day at random from the days that rose, and one from the days that did not. AUC is the
> probability the model assigned the riser a higher score.

That equivalence is what makes the number interpretable. `y_large_move_next` scores ~0.57 here,
which means: shown a real large-move day and a quiet day, the model ranks them correctly 57% of
the time against 50% for a coin. Stated that way, "a real but modest edge" stops being a
euphemism — seven percentage points on a pairwise comparison is exactly what a small edge looks
like.

**Why this metric and not accuracy.** Both rates above are normalised *within* their own class, so
the ratio of positives to negatives cannot move the curve. That matters enormously for
`y_large_move_next`, whose base rate is ~6.7%: a model that mindlessly predicts "no large move"
every single day is 93.3% accurate and completely worthless. Accuracy rewards it; AUC gives it
0.5, correctly. `sql/40_analytics_labels.sql` produces one balanced-ish label and one heavily
imbalanced one, and AUC is the metric that can score both on the same scale.

**Values below 0.5** mean the ranking is systematically backwards — informative in principle, since
inverting the predictions would score above 0.5. In practice, at this project's sample sizes, they
are noise: the per-symbol table has XLE at 0.461 on direction, which is a coin landing badly across
five folds rather than a discovery.

**What AUC deliberately cannot tell you**, which is why §7 has three checks rather than one:

- *Whether the probabilities are honest.* AUC depends only on the **order** of the scores, so any
  strictly increasing transformation leaves it untouched. Halve every probability and AUC is
  identical. This is a feature — it isolates ranking from calibration — but it means a model can
  post a respectable AUC while stating probabilities that are badly wrong, which is precisely this
  project's situation in the 0.55–0.60 bucket.
- *Whether the threshold you actually trade is any good.* AUC averages over all of them, including
  ones nobody would use. Hence the confusion matrix at 0.55.
- *Whether the edge is worth money.* This is the hard-won one. `y_large_move_next` ranks well at
  0.57 and still loses to buy-and-hold once costs and the label's unsigned nature are accounted for
  (§8). **A better ranking is not the same as a better strategy**, and no AUC could have revealed
  that — only the backtest did.

**Why a value near 0.5 is the expected outcome, not a failure.** Daily equity direction is close
to a random walk. A realistic, honest edge from price-derived features lands around **0.52–0.56**.
Small edges are what real quantitative signals look like; they compound across many trades rather
than winning big on any one. The suspicious direction is the *opposite* one — a simple logistic
regression on six basic features scoring 0.65+ is far more likely to indicate leakage or a lucky
split than genuine alpha. That expectation is written into the leakage-audit procedure as a
trigger for investigation, and it has already earned its place: the ingestion corruption in §9 was
caught precisely because large-move AUC jumped to 0.67 and that was treated as a symptom rather
than a success.

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

Walk-forward, 5 folds, 15 ETFs, 2018 to present, 32,295 training rows
(2,153 dates × 15 instruments):

| Target | Mean ROC-AUC | Std | Range |
| --- | --- | --- | --- |
| `y_up_next_day` | 0.5106 | 0.0160 | 0.4867 – 0.5310 |
| `y_large_move_next` | 0.5744 | 0.0413 | 0.5337 – 0.6404 |

The original 3-ETF run (SPY, TLT, XLE; 6,438 rows) is kept here because the comparison is
itself a result: `y_up_next_day` was 0.4934 ± 0.0279 and `y_large_move_next` 0.5928 ± 0.0636.
Five times the rows moved the large-move **mean** by 0.018 while tightening its **spread** by
about a third. That is exactly what §10 predicts — correlated instruments sharing a calendar
add far less independent information than the row count suggests, so a bigger universe buys
precision rather than a different answer.

**Direction: no signal.** The distribution straddles 0.5 across every fold. These six
price-derived features do not predict next-day direction. This is the expected result in a
liquid, efficient market — and it confirms Phase A's single-split figure was not an unlucky draw.

**Per-symbol, the large-move edge is broad rather than carried by a few instruments.** Fourteen
of fifteen symbols clear 0.5, from GLD at 0.512 to SPY at 0.665, which is the more reassuring of
the two possible shapes — a pooled 0.574 produced by three strong assets and twelve coin flips
would be a much weaker result wearing the same number. Direction stays uninformative
instrument by instrument too, ranging 0.461 to 0.571 around a pooled 0.510.

Resist reading the per-symbol ranking as a finding. Fifteen symbols scored over five folds is
fifteen chances for noise to look like structure, the per-symbol fold-to-fold spread is wide
(often ±0.08 to ±0.18, far wider than the pooled spread), and nothing here has been corrected for
multiple comparisons. The table is evidence about *shape* — broad versus concentrated — not a
ranking to select instruments from. Selecting on it would be the same trap §11 refuses for
hyperparameters, applied to assets.

The table also serves a second purpose that has already paid off once. Genuinely different
instruments produce visibly different scores, so a wall of near-identical rows means the assets
are not actually different — the signature of the ingestion bug in §9, which every per-asset
assertion passed cleanly. It is the model layer's standing check against that class.

**Large moves: a real but modest edge.** Every fold clears 0.5, mean 0.57. This makes mechanical
sense: `vol_20d` and `drawdown_60d` are literally volatility and stress measures, so "will
tomorrow be a large move" is a far more natural question for this feature set than "which
direction". The spread is still wide enough (0.53–0.64) to be the kind of instability a single
split would have hidden, though narrower than it was on three instruments.

### What the backtest adds

Ranking quality is not profitability, and Phase D is what separates them. Scored against
`always_long` — holding the universe unconditionally — neither strategy is worth running:

| Strategy | Sharpe | Total return | Max drawdown | Trades | vs. benchmark |
| --- | --- | --- | --- | --- | --- |
| `always_long` | 1.48 | 8.3% | −7.2% | 75 | — |
| `large_move_filter` | 1.22 | 5.7% | −6.9% | 898 | −0.26 Sharpe |
| `direction_threshold` | 0.66 | 2.1% | −2.5% | 1,221 | −0.82 Sharpe |

`direction_threshold` is the arithmetic consequence of a 0.51 AUC meeting transaction costs: 1,221
trades to underperform doing nothing. Its low drawdown is not risk management, it is being absent
from the market most of the time.

`large_move_filter` is the more informative failure, because it uses the one signal that does
exist. Volatility *is* predictable here at ~0.57 AUC, yet stepping aside on predicted-large-move
days bought a 0.3-point drawdown improvement for 2.6 points of return. The reason is in the label:
`y_large_move_next` is unsigned, so it cannot distinguish a crash from a rally, and avoiding one
forgoes the other. **Being right about volatility is not the same as having an edge** — a
directional signal, or a signed magnitude label, is what would be required to convert it.

This is the point of building the instrument before believing anything. The 0.57 AUC was real and
survived walk-forward validation; it still does not produce a strategy. Nothing short of a backtest
would have shown that.

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
| `sql/10_validations.sql` | raw **input**: nulls, negative prices, `high < low`, duplicate keys, future dates, assets with no prices, duplicate series across symbols | runs inside `make run`, before staging |
| `sql/90_assertions.sql` | computed **output**: re-derives each value from its definition | runs in `make test`, needs a built warehouse |
| `tests/test_folds.py` | fold construction logic | pytest, no database, milliseconds |
| `tests/test_ingestion.py` | what may enter the warehouse (settled sessions only) | pytest, no database or network |
| `tests/test_evaluation.py` | per-symbol scoring and undefined-AUC handling | pytest, no database, milliseconds |

The split by cost is deliberate: pytest runs on every edit, the SQL assertions run after a
pipeline rebuild.

### The blind spot all of them shared

Every check listed above validated **within a single asset**. None compared assets to each
other, and that gap turned out to be large enough to drive a truck through.

When price fetching was made concurrent, the obvious implementation — calling `yf.download()`
inside each task — was silently wrong. `yf.download()` stages its results in module-level state
(`yfinance.shared._DFS`) which it *resets on entry*, so concurrent calls overwrite each other's
accumulator and every caller can receive the same, wrong ticker's frame. The warehouse ended up
holding one ticker's prices under all fifteen symbols.

Nothing failed. Each duplicated series is internally consistent, so `ret_fwd_1d(t) == ret_1d(t+1)`
held perfectly, per-asset row counts were right, no NULLs appeared, signs were sane — all six
assertions passed, and the model reported a *better* large-move AUC (0.67) than the honest
number. A plausible improvement is the most dangerous possible symptom. It was caught only by
noticing that SHY, a 1–3 year Treasury fund, reported the same volatility as XLE to five decimal
places.

The fix is `yf.Ticker().history()`, which keeps its result on the instance rather than in module
state. The *lesson* is the check that now sits in `sql/10_validations.sql`: fingerprint each
symbol's series and reject duplicates, because two distinct instruments cannot have identical
OHLCV histories. It is the first validation here that looks **across** assets, and it exists
because the audit found something no assertion covered — the ratchet described below, working as
intended.

The same blind spot has a second shape, and it comes from a deliberate choice. Fetching is
per-ticker fault-tolerant: `prepare_data.py` catches each download's failure so one bad symbol
cannot abort the batch. That is the right behaviour for a network job, but it converts a loud
failure into a quiet one — the asset still loads into `raw.assets` from `config/assets.csv`, just
with nothing behind it. Every check that existed tested a property *of* price rows, and zero rows
satisfies all of them vacuously; the warm-up assertion inner-joins prices, so a price-less asset
is skipped rather than flagged. A universe of fifteen could silently become fourteen with the
whole suite green. So `10_validations.sql` also asserts that every configured symbol has at least
one price row.

Both checks generalise to the same rule, worth stating once: **assertions that test the properties
of rows cannot see rows that are missing, or rows that are duplicated from elsewhere.** Anything
guarding *presence* and *distinctness* has to be written separately and deliberately.

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

**The arrangement has now paid for itself, and it is worth being precise about which half did the
work.** The ingestion corruption described above — every symbol carrying one ticker's prices — was
not caught by a test. It could not have been: 18 pytest cases passed, all six SQL assertions
passed, row counts were correct, and no NULLs appeared, because every one of those checks
validated a property that a duplicated series satisfies perfectly. The ratchet was green and
wrong.

What caught it was the audit procedure's habit of *sanity-checking the numbers against what they
should plausibly be* rather than only against what the code computes. The skill instructs that a
suspiciously good result is a lead to investigate, not a win — and the run had just produced a
large-move ROC-AUC of 0.67 against a recorded honest baseline near 0.59. Pulling on that thread
meant comparing volatility across assets, where a 1–3 year Treasury fund and an energy fund
reporting identical figures to five decimal places is impossible on its face.

That is the frontier doing precisely the job it exists for: no assertion covered it, because
nobody had thought to write one, and the only thing standing between a corrupt warehouse and a
published result was a procedure that asks whether the output is *believable*. Both new checks in
`sql/10_validations.sql` exist because of that pass, which is the ratchet absorbing the class so
the audit never has to catch it again.

A `PostToolUse` hook (`.claude/hooks/comment_reminder.sh`) completes the loop by prompting for the
*reasoning* behind edits to `.py`/`.sql` files. In a pipeline like this a wrong comment is cheap;
a wrong assumption about what a window frame may see at time `t` is expensive. The hook is
strictly advisory and always exits zero — a hook able to break the edit loop over a style concern
would be a worse trade than an occasional missing comment.

---

## 10. Known limitations and open issues

Stated plainly, because a limitations section that reads like marketing is worthless.

**The universe is 15 ETFs, and still narrower than it looks.** 32,295 rows sounds substantial
but is 2,153 dates × 15 instruments that mostly share one calendar and one market beta — five
of them are US equity sector funds whose correlation with SPY runs 0.56 to 0.94. It is a
genuine improvement on the original three (see §8: the estimates got more precise without
moving), but the honest read is that the number of independent observations is still much
closer to the number of trading dates than to the row count. There are no individual equities
yet, and nothing outside US-listed ETFs.

**Row count will always overstate independent information.** Assets trade on the same days and
share market beta, so N assets on one date are nowhere near N independent observations — the
effective sample size is far closer to the number of trading dates. Expanding the universe should
be expected to tighten the *variance* of estimates more than to move their *means*.

**Events are not implemented.** `raw.events` and `staging.events` exist in the schema, but
`prepare_data.py` writes an empty (schema-valid) `events.csv`. `staging.event_asset_map` is
populated by a `CROSS JOIN` mapping every event to every asset at weight 1.0 — a placeholder
explicitly labelled as such in the file, not a mapping rule. The project is named for
event-driven analysis and does not yet do any.

**The backtest exists, but its cost model is one number.** Costs are a single `--cost-bps`
charged on position changes, defaulting to 1.5bp. Commissions are effectively zero for retail
now, so what that number stands for is the **bid-ask spread**: you buy at the ask and sell at the
bid, and commission-free brokers recover it through payment for order flow rather than waiving
it. The magnitude varies more across this universe than a single figure admits — a penny spread
is ~0.13bp on SPY near $765 but ~3.6bp on UUP near $28 — and it widens in exactly the conditions
`large_move_filter` trades into. It also does not scale with size and assumes fills at the close.

Whether that imprecision matters is testable, so it was tested rather than argued:

| Mean Sharpe | 0bp | 1.5bp | 5bp |
| --- | --- | --- | --- |
| `always_long` (75 trades) | 1.4833 | 1.4806 | 1.4743 |
| `large_move_filter` (898) | 1.2510 | 1.2178 | 1.1402 |
| `direction_threshold` (1,221) | 0.7798 | 0.6576 | 0.3727 |

The conclusion is unchanged at **zero** cost: `direction_threshold` still loses to buy-and-hold by
0.70 Sharpe when trading is free. So the cost assumption is not load-bearing for anything claimed
here. What the table does show is that sensitivity tracks turnover — across 0 to 5bp the benchmark
moves 0.6% and the high-turnover rule loses 52% of its Sharpe. The parameter is really a turnover
tax, and it will decide the verdict for any future strategy that looks marginally profitable.

Two larger costs are **not modelled at all**, and both would make results worse rather than
better: taxes (1,221 trades generate short-term gains, and in some jurisdictions frequent trading
is treated as business income — tens of percent against basis points) and borrow costs on the
short leg of `direction_threshold`. The current backtest is therefore generous to the model, and
it still says do not trade this.

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

**Not re-keying the experiment log to survive rebuilds.** As §2 describes, `make run` clears
`model_runs` and `model_predictions`, because `asset_id` is a surrogate key regenerated by every
staging rebuild and predictions carrying stale ones would be misattributed rather than merely old.
Re-keying `model_predictions` on `symbol` would fix that properly: symbols are stable natural
identifiers, so the log could then outlive the warehouse it was produced against.

It is not done yet because the value is currently near zero and the cost is not. Nothing compares
runs across rebuilds today — training is run, results are read, and the next rebuild is a fresh
start. Meanwhile the change touches the schema, the training script's inserts, assertion 6 in
`sql/90_assertions.sql`, and whatever the Phase D backtest ends up reading, which is a lot of
surface to disturb while the backtest itself is still unwritten. There is also a subtlety that
would need deciding rather than defaulting: a prediction that survives a rebuild is only
meaningful if the features behind it were computed the same way, so the log would need to record
enough about the pipeline version to say whether an old row is still comparable. `git_commit`
already gestures at this and would have to become load-bearing.

The trigger to revisit is concrete: the first time a genuine question needs runs compared across a
rebuild — an event-feature model in Phase E measured against a baseline trained weeks earlier, say
— rather than any general preference for durable history.

**Not enforcing documentation upkeep with a hook.** Docs in this project go stale in a specific
way: a change lands, and the sections describing measured results, limitations, or the build log
keep describing the system as it was. It has happened repeatedly, so mechanising it is tempting,
and a hook is the obvious instrument — it cannot be forgotten, which is the actual failure mode.

The tempting version is a `Stop` hook that compares `git diff --name-only` and complains if `.py`
or `.sql` changed while no `.md` did. It should not be built, because it checks a **proxy** for
the property that matters. Touching any markdown file satisfies it; adding a blank line satisfies
it. It cannot tell a results table updated with new fold numbers from one left describing a
three-asset warehouse that no longer exists. By this document's own standard for assertions —
§9's rule that a check which passes unconditionally is *worse* than no check, because it
manufactures confidence — such a hook is a bad assertion, and it would be a bad assertion
guarding the very thing it is supposed to keep honest.

The distinction worth keeping is that **a hook can prompt, but it cannot verify.** It runs before
the work is inspected and has no way to read intent, so its honest job is to raise a question at
the right moment. `hooks/comment_reminder.sh` is scoped exactly that way, and is careful to say
so: it asks for the reasoning behind an edit and always exits zero.

Judging whether a change is *documented* requires knowing which numbers moved, whether §8's
results are now stale, whether a limitation in §10 has been resolved or merely reduced, and
whether the build log needs a row — all judgement, which is what a skill encodes and a hook
structurally cannot. That places it on the frontier side of §9's split, alongside the leakage
audit rather than alongside the assertions.

So it is `.claude/skills/doc-audit/`. It maps each kind of change to the sections that go stale
because of it, and — the part a hook could never reach — checks the claims against the warehouse
rather than against the diff, querying row and asset counts and comparing them to the figures the
documents assert. It also carries a "what is not a finding" section, because an audit that cries
wolf gets skipped, and a skipped audit is worth exactly as much as the hook it replaced.

**Not ingesting live intraday data — batch, end-of-day only, for now.** Yahoo serves the current
session as an ordinary bar while it is still forming, so a fetch at 13:00 returns a partial close
and roughly a third of the day's volume. `prepare_data.py` drops it: only sessions before today's
date in market time are ingested.

The rule deliberately does *not* ask whether the 16:00 close has passed. "Keep today's bar after
the close" would make a 15:00 rebuild and a 17:00 rebuild disagree, which is precisely the
non-determinism being removed — and determinism is the property §2 gives up incremental updating
to protect. The price is that a settled session waits until the next day to be ingested. The
alternative price was a warehouse whose contents depend on what time you happened to run it, plus
one silently wrong label per asset, since `ret_fwd_1d` on the second-to-last row would have been
measured against a close that had not happened yet.

The intended direction is a warehouse that refreshes continuously, potentially every second. That
is a genuinely different system rather than a faster version of this one. A forming bar is not a
fact but a value that keeps changing, so every row would need to carry the time at which it was
true, and every feature would need to be answerable as "what did this look like *as of* 13:04:22"
rather than "what does this look like now". That is the bitemporal problem, and the current
point-in-time discipline — one row per asset per settled session, features strictly trailing — is
the right foundation to grow into it rather than something that would have to be undone. Until
then, the honest description is a daily batch pipeline that never sees a partial day.

**Not implementing events before the baseline was validated.** Events are the project's headline
concept, and building them first was the tempting order. But without a working baseline there
would be no way to measure whether event features added anything — the point of a baseline is to
be the thing the next increment is measured against. Event ingestion (alignment, mapping, missing
data, time zones) is also the highest-uncertainty work in the roadmap; doing it before the
evaluation machinery existed would have meant debugging two hard problems at once.

---

## 12. Reading order for the rest of the documentation

- **`README.md`** — what the system does, how to run it, the phase roadmap and its status.
- **`docs/course_calibration_and_events.md`** — calibration and the concepts behind Phase E:
  event alignment, surprise, the schedule-versus-outcome split, and multiple comparisons.
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
| 2026-08-24 | `0bed761` | Features and labels switched from `close` to `adj_close` — the fix for the "unadjusted close" limitation then flagged in §10, verified safe by the leakage-audit skill and a clean `sql/90_assertions.sql` run. Deliberately ahead of the universe expansion, which would otherwise have let stock splits corrupt `vol_20d`/`drawdown_60d`. |
| 2026-08-26 | `bbddd5b` | Universe moved to `config/assets.csv` and expanded 3 → 15; prices fetched concurrently with `asyncio`. Uncovered a silent ingestion bug (`yf.download()`'s module-level shared state races, handing every ticker the same frame) that all six assertions passed straight through, because each validated *within* one asset and none looked *across* them. `sql/10_validations.sql` now fingerprints each symbol's series — see §9. |

| 2026-08-27 | *(pending)* | **Phase D**: `python/backtest_from_predictions.py`. Neither strategy beats `always_long` (Sharpe 1.48): the direction rule returns 0.66 and the large-move filter 1.22. Writing its tests exposed a drawdown bug — the running peak started at the first day's equity rather than at the initial capital, so any decline beginning on day one reported as zero drawdown. |
| 2026-08-27 | *(pending)* | Phase D schema: `analytics.backtest_runs` added and `backtest_results` re-keyed onto it. Summary metrics (Sharpe, max drawdown, hit rate, expectancy) are one scalar per run and had no home in a table of daily rows; the assumptions that produce them (strategy, `cost_bps`) were not recorded at all. Mirrors the `model_runs`/`model_predictions` split for the same reason. |

The gap between March and August is not a documented decision — just time away from the project.
Everything from Phase A onward happened in one concentrated stretch, which is why those entries
read as a single coherent arc while the March commits are more piecemeal setup.
