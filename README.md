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
-   SQL (window functions, rolling statistics)
-   Make
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

Trains a logistic regression on `analytics.v_training_dataset` (chronological train/test
split) and writes predictions to `analytics.model_predictions`, tracked by a new row in
`analytics.model_runs`. Not part of `make run` — each invocation appends a new model run
rather than overwriting, so it's kept as a separate, explicit step. See "Implementation
Roadmap" below (Phase A) for details.

------------------------------------------------------------------------

# Project Structure

```
EDMP_Engine/
├── data_raw/       # generated CSVs (ignored by git)
├── docs/
│   └── architecture/   # warehouse schema diagram (ERD source + SVG)
├── python/
│   ├── prepare_data.py
│   └── train_baseline_logreg.py
├── sql/
│   ├── 00_schema.sql
│   ├── 10_validations.sql
│   ├── 20_staging_transform.sql
│   ├── 30_analytics_features.sql
│   ├── 40_analytics_labels.sql
│   └── 50_training_dataset.sql
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

# Planned extensions

Price ingestion, warehousing, features, time-safe labels, and a baseline logistic
regression model (Phase A) are done. See "Implementation Roadmap" below for the
step-by-step plan and the status of each phase.

**Next up — validate and backtest the baseline:**
- Walk-forward time-series validation (not a single split) + honest evaluation (calibration, naive-baseline comparison)
- Backtesting engine with costs/slippage + risk metrics (Sharpe, max drawdown, hit rate)

**After a baseline exists — events:**
- Macroeconomic event alignment logic (timezone/after-hours → effective trading date)
- Event → asset mapping rules (replace the `staging.event_asset_map` cross-join baseline)
- Event-conditional features (pre/post windows, surprise magnitude, sentiment proxies)

**Later:**
- Power BI dashboards (data health, predictions, backtest performance, slices by event type)
- Uncertainty estimates on predictions (confidence intervals via bootstrap / Bayesian logistic regression)
- Gradient-boosted trees (XGBoost/LightGBM) if event interaction terms exceed what logistic regression can capture

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

## Phase B — Time-series validation
- Walk-forward / expanding-window validation instead of a single split: train on `[start, t]`, test on `[t, t+k]`, slide forward, repeat
- Purging/embargo around the train/test boundary: drop rows whose rolling feature windows (`vol_20d`, `mom_20d`, `drawdown_60d`) overlap the other side
- A plain random split (sklearn's default) is invalid here — adjacent rows share overlapping lookback windows

## Phase C — Evaluate honestly
- Confusion matrix at the chosen probability threshold, not just accuracy
- Calibration curve — does the ~0.6 probability bucket actually resolve up ~60% of the time?
- Compare against the naive baseline (always predict the majority class / base rate)

## Phase D — Backtest
- `python/backtest_from_predictions.py`: turn `p_up` into a position (threshold rule, or size proportional to `p_up - 0.5`)
- Daily strategy return = `position * ret_fwd_1d`, minus transaction costs
- Sharpe ratio (`mean(daily_returns) / std(daily_returns) * sqrt(252)`), max drawdown, hit rate vs. expectancy
- Write results to `analytics.backtest_results`, keyed by `model_run_id`

## Phase E — Events (only after Phases A–D produce a baseline)
- Ingest in order of ease: macro calendar (CPI/FOMC, quantifiable via `actual - forecast` surprise) → earnings (per-ticker mapping, after-hours handling) → speeches/text (NLP, sentiment proxy)
- Effective trading date alignment: map each event to pre-market / after-market / weekend → next tradable session
- Event-conditional features: days-since-last-event, event-day dummy variables, interaction terms (e.g. `mom_20d * is_fomc_day`)
- Multiple comparisons risk: hold out an event-feature test set, or apply a correction (e.g. Bonferroni), before trusting any single new event feature
- Replace the `staging.event_asset_map` cross-join placeholder with real event → asset mapping rules

## Phase F — Beyond logistic regression (later)
- If event interaction terms make the linear model insufficient, move to gradient-boosted trees (XGBoost/LightGBM) — trades away the current clean coefficient interpretability for automatic interaction/nonlinearity handling

------------------------------------------------------------------------

# Possible Additions

- **Concurrent price ingestion**: `python/prepare_data.py` currently downloads tickers sequentially via `yf.download()`, which is I/O-bound — each call spends most of its time waiting on the network. Wrapping the fetch loop in `asyncio.TaskGroup` + `asyncio.to_thread` (yfinance isn't natively async) with a `Semaphore` capping concurrency (~20-25 at once, to stay under Yahoo's informal rate limits) would cut a ~200-ticker fetch from roughly 90-120s sequential to ~8-15s concurrent, with no changes to feature engineering or modeling. Training (`train_baseline_logreg.py`) stays untouched since it's CPU-bound — a `ProcessPoolExecutor` would be the equivalent change there, but isn't needed now.

------------------------------------------------------------------------

# Notes

-   data_raw/ is ignored in Git
-   .venv/ is ignored in Git
-   PostgreSQL must be running locally
-   Designed for Linux / Ubuntu environments
