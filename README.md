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
analytics.features_daily analytics.labels_daily

The pipeline is orchestrated via Makefile and fully rebuildable from scratch.

------------------------------------------------------------------------

# Tech Stack

-   PostgreSQL
-   Python (yfinance, pandas)
-   SQL (window functions, rolling statistics)
-   Make
-   Virtual environment (venv)

------------------------------------------------------------------------

# Setup

## 1. Clone the repository

```
git clone <repo-url>
cd EDMP_Engine
```

## 2. Create Python virtual environment

```
python3 -m venv .venv 
source .venv/bin/activate pip install -r
requirements.txt
```

## 3. Create PostgreSQL database

createdb edmp_engine

If you encounter a role error:

```
sudo -u postgres createuser --superuser \$USER
```

## 4. Build Everything

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

------------------------------------------------------------------------

# Project Structure

```
EDMP_Engine/
├── data_raw/       # generated CSVs (ignored by git)
├── python/
│   └── ingest_yahoo_daily.py
├── sql/
│   ├── 00_schema.sql
│   ├── 10_raw_load.sql
│   ├── 20_staging_transform.sql
│   ├── 30_analytics_features.sql
│   └── 40_analytics_labels.sql
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

- Macroeconomic event alignment logic (timezone/after-hours → effective trading date)
- Event → asset mapping rules (replace cross-join baseline)
- Event-conditional features (pre/post windows, surprise magnitude, sentiment proxies)
- Training dataset view (`analytics.v_training_dataset`) with strict null filtering
- Baseline probabilistic model (logistic regression) + calibration (Platt / isotonic)
- Time-series validation (walk-forward) + metrics (ROC-AUC, Brier score, calibration)
- Store predictions in `analytics.model_predictions` with `analytics.model_runs` tracking (git commit, date ranges)
- Backtesting engine with costs/slippage + risk metrics (Sharpe, max drawdown)
- Power BI dashboards (data health, predictions, backtest performance, slices by event type)
- Uncertainty estimates on predictions (confidence intervals via bootstrap / Bayesian logistic regression)

------------------------------------------------------------------------

# Notes

-   data_raw/ is ignored in Git
-   .venv/ is ignored in Git
-   PostgreSQL must be running locally
-   Designed for Linux / Ubuntu environments
