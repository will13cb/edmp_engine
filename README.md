# EDMP Engine

**Event-Driven Market Probability Engine**

A fully reproducible historical market reaction pipeline that:

-   Ingests real market data (Yahoo Finance)
-   Stores data in PostgreSQL (raw → staging → analytics layers)
-   Computes quantitative features (returns, volatility, momentum,
    drawdown)
-   Generates forward-looking labels
-   Prepares structured datasets for probabilistic modeling and
    backtesting

This project mirrors the architecture of a real-world event-driven
trading system --- without live execution complexity.

📖 **[docs/design_decisions.md](docs/design_decisions.md)** explains the reasoning behind the
architecture, the validation discipline, the test suite, and the honest limitations — including
why a near-0.50 ROC-AUC on next-day direction is the *expected* result rather than a failure.

------------------------------------------------------------------------

# Architecture

Yahoo Finance (Python)  
↓  
CSV (data_raw/)  
↓  
PostgreSQL raw.*  
↓  
staging.*  
↓  
analytics.features_daily / analytics.labels_daily  
↓  
analytics.v_training_dataset  
↓  
analytics.model_runs / analytics.model_predictions (python/train_baseline_logreg.py)

The pipeline is orchestrated via Makefile and fully rebuildable from scratch.

------------------------------------------------------------------------

# Tech Stack

-   PostgreSQL
-   Python (yfinance, pandas, psycopg, scikit-learn)
-   SQL (window functions, rolling statistics, `DO $$` assertion blocks)
-   Make
-   pytest
-   Virtual environment (venv)

------------------------------------------------------------------------

# Setup

## 1. Clone the repository

```
git clone https://github.com/will13cb/edmp_engine.git
cd edmp_engine
```

## 2. Create Python virtual environment

```
python3 -m venv .venv 
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Create PostgreSQL database
Install and start PostgreSQL
macOS (Homebrew)
```
brew install postgresql
brew services start postgresql
```

Ubuntu / Debian
```
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo service postgresql start
```

## 4. Create the database

createdb edmp_engine

## 5. Build Everything

```
make run
```

This will:

1.  Create schemas and tables
2.  Truncate raw tables
3.  Load CSV data
4.  Transform raw → staging
5.  Compute analytics features
6.  Compute forward labels

The system is deterministic and rebuildable.

## 6. Train the baseline model (optional)

```
make train_baseline
```

Trains a logistic regression on `analytics.v_training_dataset` (walk-forward folds, never a
random split) and writes predictions to `analytics.model_predictions`, tracked by a new row in
`analytics.model_runs` per fold. Not part of `make run` — each invocation appends new model runs
rather than overwriting, so it's kept as a separate, explicit step. See "Implementation
Roadmap" below (Phase A) for details.

Note that `make run` **clears** these tables: they accumulate across training runs but reset
whenever the warehouse beneath them is rebuilt, because `model_predictions.asset_id` points into
a `staging.assets` table that is regenerated with new ids each time. Run training after a
rebuild, not before, and `pg_dump` the two tables first if a particular run has to outlive one
(`docs/design_decisions.md` §2).

------------------------------------------------------------------------

# Project Structure

```
EDMP_Engine/
├── config/
│   └── assets.csv  # the asset universe: tracked, hand-edited, loaded into raw.assets
├── data_raw/       # generated CSVs (ignored by git)
├── docs/
│   ├── architecture/   # warehouse schema diagram (ERD source + SVG)
│   ├── design_decisions.md                    # why the system is built this way
│   └── course_validation_and_backtesting.md   # concepts behind Phases B-D
├── python/
│   ├── prepare_data.py
│   └── train_baseline_logreg.py
├── sql/
│   ├── 00_schema.sql
│   ├── 10_validations.sql          # guards raw INPUT (fail-fast, pre-staging)
│   ├── 20_staging_transform.sql
│   ├── 30_analytics_features.sql
│   ├── 40_analytics_labels.sql
│   ├── 50_training_dataset.sql
│   └── 90_assertions.sql           # guards computed OUTPUT (run by `make test`)
├── tests/
│   ├── conftest.py
│   ├── test_folds.py               # fold / embargo invariants (pytest)
│   ├── test_ingestion.py           # only settled sessions may enter the warehouse
│   └── test_evaluation.py          # per-symbol scoring; undefined-AUC handling
├── .claude/
│   ├── settings.json               # registers the PostToolUse hook
│   ├── hooks/comment_reminder.sh   # prompts for why-comments on .py/.sql edits
│   ├── skills/leakage-audit/       # on-demand temporal-leakage audit procedure
│   └── skills/doc-audit/           # checks a change is reflected in the docs, not just alongside them
├── Makefile
├── requirements.txt
├── README.md
└── .gitignore
```

------------------------------------------------------------------------

# Features Computed

From historical daily prices:

-   Daily return (ret_1d)
-   Log return (logret_1d)
-   Rolling 20-day volatility (vol_20d)
-   5-day momentum (mom_5d)
-   20-day momentum (mom_20d)
-   60-day drawdown (drawdown_60d)

------------------------------------------------------------------------

# Labels Generated

-   Forward 1-day return
-   Absolute forward return
-   y_up_next_day
-   y_large_move_next (volatility-scaled threshold)

------------------------------------------------------------------------

# Reproducibility

The pipeline is designed to:

-   Be fully rebuildable from zero
-   Avoid manual pgAdmin steps
-   Avoid hidden state
-   Use deterministic truncation before rebuild

To rebuild cleanly:

make run

------------------------------------------------------------------------

# Concept

The objective of this project is to estimate:  
P(market moves \| historical features + event context)

In practical terms:

The probability that the market moves, given past data and current event information.

Inputs:  
-   Historical features = past prices, returns, volatility, volume, technical indicators, etc.

-   Event context = news, earnings, speeches, macroeconomic announcements, or other real-world events.

Output:  
-   The model learns patterns from historical data and event-driven signals, and produces a probability estimate.  
For example:
0.63 → a 63% probability that the market moves up, given the chosen factors, not an absolute forecast of the market.

Interpretation:  
Markets incorporate vast and continuously evolving information. The model observes only a defined subset of that information. As a result, its output represents a conditional probability, not a deterministic prediction.
Financial markets are inherently stochastic systems influenced by noise, randomness, and unexpected events.  
Rather than asserting a direction, the model estimates how likely a move is, given the available feature set.

Probabilistic outputs allow for:
-   Rational decision-making
-   Risk management and position sizing
-   Expected value calculations
-   Operating optimally under uncertainty

In essence, the system evaluates whether the selected variables contain directional signal, without claiming to model the full complexity of market dynamics.

------------------------------------------------------------------------

# Time-Series Validation Protocol

This project uses strict temporal validation to prevent lookahead bias.

-   All features at time ( t ) are constructed exclusively from
    information available at or before ( t ).
-   Forward returns and classification labels are computed using ( t+1 )
    prices and stored separately.
-   Training and test datasets are split chronologically, not randomly.
-   The model is trained on historical data up to a cutoff date and
    evaluated on strictly future observations.

## Formal Definition

Features at time t:

X_t = f({ data_τ : τ ≤ t })

Label:

y_t = outcome_{t+1}

All features at time t are constructed using information available at or before t.
No future information is used in feature construction or model training.

------------------------------------------------------------------------

# Enforcing that protocol

The protocol above is only worth stating if something enforces it. Temporal leakage is a
*silent* failure: nothing crashes, no row count changes, and the only symptom is a metric that
quietly becomes optimistic. So the guarantees are mechanical rather than aspirational.

```bash
make test     # pytest (fast, no DB), then sql/90_assertions.sql (needs a built warehouse)
```

## Two validation layers, guarding opposite ends

| File | Guards | Runs |
| --- | --- | --- |
| `sql/10_validations.sql` | raw **input** — nulls, negative prices, `high < low`, duplicate keys, future dates, assets with no prices, identical series across symbols | inside `make run`, before staging |
| `sql/90_assertions.sql` | computed **output** — re-derives each value from its definition | `make test`, after the pipeline |

The strongest assertion is `ret_fwd_1d(t) == ret_1d(t+1)`. Both sides equal
`close(t+1)/close(t) - 1` by definition, so any off-by-one shift in a `LEAD`/`LAG`, or a window
frame that lost its `PARTITION BY asset_id`, makes them diverge. `sql/90_assertions.sql` also
asserts **at the database level** that every `model_predictions` row falls inside its run's test
window — turning "we only store out-of-sample forecasts" from a convention in Python into a
structural guarantee that a later backtest cannot silently violate.

`tests/test_folds.py` covers the walk-forward logic, where an overlapping train/test window
would invalidate every metric while raising no error: no overlap, an embargo gap of exactly
`EMBARGO_DAYS` trading days, disjoint test blocks, and a genuinely expanding window.

**The assertions are verified to fail.** A suite that has never failed is unverified, so each
invariant was confirmed by deliberately breaking it and watching it fire. An assertion that
passes unconditionally is worse than none, because it licenses false confidence.

## Ratchet and frontier

Automated tests catch the invariants someone has already written down. They cannot catch a
*novel* mistake — a new feature using `LEAD`, a scaler fit outside the fold loop (which leaves
no trace in the database at all). That gap is covered by an on-demand audit procedure
(`.claude/skills/leakage-audit/`) encoding this project's specific rules and known subtleties,
such as why retroactively-restated adjusted prices are safe in a *ratio* but not in a *level*.

The two feed each other: when the audit finds something, it becomes a new assertion, so the
ratchet absorbs it and the audit never has to catch that class again.
`test_embargo_covers_the_longest_feature_lookback` is exactly that — it began as a judgement
call ("did anyone raise `EMBARGO_DAYS` after adding a longer window?") and is now a test that
parses lookbacks out of the feature names and fails on its own.

**This has already paid off once, and the half that did the work was the audit, not the tests.**
The ingestion bug that left every symbol holding one ticker's prices was invisible to the
ratchet — 18 pytest cases passed, all six SQL assertions passed, row counts were right and no
NULLs appeared, because a duplicated series satisfies every one of those properties. The suite
was green and the data was wrong. What caught it was the audit's rule that a suspiciously good
result is a lead rather than a win: large-move ROC-AUC had jumped to 0.67 against a recorded
baseline near 0.59, and pulling on that turned up a Treasury fund and an energy fund reporting
identical volatility to five decimal places. Both new checks in `sql/10_validations.sql` exist
because of that pass — the ratchet absorbing the class so the audit never has to find it again.

A `PostToolUse` hook (`.claude/hooks/comment_reminder.sh`) rounds this out by prompting for the
*reasoning* behind edits to `.py`/`.sql` files — in a pipeline like this a wrong comment is
cheap, but a wrong assumption about what a window frame may see at time `t` is expensive. Note
what the hook does and does not do: it *prompts*, it does not *verify*. A hook can see that a
file was touched; it cannot judge whether what was written is true or complete. That distinction
is why documentation upkeep belongs in a skill rather than a hook — see
`docs/design_decisions.md` §11.

------------------------------------------------------------------------

# Planned extensions

Price ingestion, warehousing, features, time-safe labels, a baseline logistic regression
model (Phase A), walk-forward validation with embargo (Phase B), and honest evaluation
(Phase C) are done. See "Implementation Roadmap" below for the step-by-step plan and the
status of each phase, and `docs/course_validation_and_backtesting.md` for the concepts
behind Phases B–D.

**Next up — backtest the baseline:**
- Backtesting engine with costs/slippage + risk metrics (Sharpe, max drawdown, hit rate)
- Fix probability calibration (Platt / isotonic) before using `p_up` for position sizing

**After a baseline exists — events:**
- Macroeconomic event alignment logic (timezone/after-hours → effective trading date)
- Event → asset mapping rules (replace the `staging.event_asset_map` cross-join baseline)
- Event-conditional features (pre/post windows, surprise magnitude, sentiment proxies)

**Later:**
- Power BI dashboards (data health, predictions, backtest performance, slices by event type)
- Uncertainty estimates on predictions (confidence intervals via bootstrap / Bayesian logistic regression)
- Gradient-boosted trees (XGBoost/LightGBM) if event interaction terms exceed what logistic regression can capture
- **Live intraday data.** The pipeline is end-of-day batch: it ingests only settled sessions and
  deliberately drops the current, still-forming bar (see `docs/design_decisions.md` §11). The
  intended direction is to refresh the warehouse continuously — down to second-level updates —
  which is a different system rather than a bigger version of this one, since a forming bar is a
  value that keeps changing and every stored number would need to record *when* it was true.

------------------------------------------------------------------------

# Implementation Roadmap

## Phase A — Baseline model (logistic regression) — done
- `python/train_baseline_logreg.py`: reads from `analytics.v_training_dataset`
- Chronological train/test split (`df[df.trading_date <= TRAIN_END]`) — never random
- `StandardScaler` fit on train only, applied to both train and test
- `LogisticRegression` for both `y_up_next_day` and `y_large_move_next`
- Inserts one row into `analytics.model_runs`, many rows into `analytics.model_predictions`
- Run via `make train_baseline`. Evaluated with **ROC-AUC**: the probability that the
  model ranks a randomly chosen positive-class day (e.g. an "up" day) above a randomly
  chosen negative-class day, across all thresholds at once. `0.5` = coin flip / no signal,
  `1.0` = perfect separation. First result (single 80/20 split, not yet walk-forward
  validated): test ROC-AUC 0.49 for `y_up_next_day` (no signal — expected for a hard,
  near-random-walk target), 0.60 for `y_large_move_next` (a real, if modest, edge from the
  volatility/drawdown features). Numbers from one split shouldn't be trusted yet — that's
  what Phase B is for.

## Phase B — Time-series validation — done
- `python/train_baseline_logreg.py` now runs walk-forward (expanding-window) validation
  instead of a single split: 5 folds, each training on all history up to its cutoff and
  testing on the block after it. Each fold writes its own `analytics.model_runs` row.
- **Purging/embargo**: the first 60 trading days after each cutoff are dropped
  (`EMBARGO_DAYS`, matching the longest lookback `drawdown_60d`). Without this, test rows
  near the boundary have `vol_20d`/`mom_20d`/`drawdown_60d` values computed partly from
  training-period prices — not full label leakage, but enough that adjacent rows aren't
  independent.
- A plain random split (sklearn's default) is invalid here — adjacent rows share
  overlapping lookback windows.
- **Result** (5 folds): `y_up_next_day` ROC-AUC mean 0.4934, std 0.0279, range 0.46–0.53 —
  straddles 0.5, confirming there is no directional signal, and that Phase A's 0.49 was not
  a one-off. `y_large_move_next` mean 0.5928, std 0.0636, range 0.52–0.68 — the volatility
  edge holds up across every fold, though with meaningful fold-to-fold variance.

## Phase C — Evaluate honestly — done
- Confusion matrix at `LONG_THRESHOLD` (0.55), calibration bucket table, and naive-baseline
  comparison print per fold. See `docs/course_validation_and_backtesting.md` section 3.
- **The naive baseline beats the model on accuracy in every fold** (e.g. fold 3: 0.5844
  naive vs 0.4156 model). The model almost never clears 0.55 (single-digit true positives
  per fold), so at that threshold it mostly abstains while ~53% of days are up anyway. This
  is the single most important honest-evaluation finding: beating 0.5 AUC and beating the
  naive baseline are not the same bar.
- **Calibration is poor in the upper buckets**: the 0.55–0.60 bucket realizes ~0.33–0.45
  rather than ~0.575, i.e. the model is overconfident exactly where it would trade. Phase D
  must not size positions proportional to `p_up` until this is fixed.

## Phase D — Backtest
- `python/backtest_from_predictions.py`: turn `p_up` into a position (threshold rule, or size proportional to `p_up - 0.5`)
- Daily strategy return = `position * ret_fwd_1d`, minus transaction costs
- Sharpe ratio (`mean(daily_returns) / std(daily_returns) * sqrt(252)`), max drawdown, hit rate vs. expectancy
- Write summary metrics to `analytics.backtest_runs` (one row per model run x strategy x cost
  assumption) and the daily series to `analytics.backtest_results`

## Phase E — Events (only after Phases A–D produce a baseline)
- Ingest in order of ease: macro calendar (CPI/FOMC, quantifiable via `actual - forecast` surprise) → earnings (per-ticker mapping, after-hours handling) → speeches/text (NLP, sentiment proxy)
- Effective trading date alignment: map each event to pre-market / after-market / weekend → next tradable session
- Event-conditional features: days-since-last-event, event-day dummy variables, interaction terms (e.g. `mom_20d * is_fomc_day`)
- Multiple comparisons risk: hold out an event-feature test set, or apply a correction (e.g. Bonferroni), before trusting any single new event feature
- Replace the `staging.event_asset_map` cross-join placeholder with real event → asset mapping rules

## Phase F — Beyond logistic regression (later)
- If event interaction terms make the linear model insufficient, move to gradient-boosted trees (XGBoost/LightGBM) — trades away the current clean coefficient interpretability for automatic interaction/nonlinearity handling

------------------------------------------------------------------------

# Notes

-   data_raw/ is ignored in Git
-   .venv/ is ignored in Git
-   PostgreSQL must be running locally
-   Designed for Linux / Ubuntu environments
