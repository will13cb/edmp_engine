# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EDMP Engine (Event-Driven Market Probability Engine) is a reproducible data pipeline, not (yet) a modeling
system. It ingests daily ETF prices from Yahoo Finance, loads them into PostgreSQL through a raw → staging →
analytics layered warehouse, computes return/volatility/momentum features, and generates forward-looking
labels for a future probabilistic model. There is no model training or backtesting code yet — the tables for
it (`analytics.model_runs`, `analytics.model_predictions`, `analytics.backtest_results`) exist in the schema
but are unused by any pipeline stage. See "Planned extensions" in README.md for the roadmap (event→asset
mapping, baseline logistic regression model, backtesting engine).

## Commands

Setup (one-time):
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
createdb edmp_engine
```

Full deterministic rebuild (the standard way to run everything):
```bash
make run
```

Individual pipeline stages (each depends on the prior stage via Make prerequisites, so `make labels` will
also run `prepare_data`, `schema`, `load_raw`, `validate`, `staging`, and `features` first):
```bash
make prepare_data       # python/prepare_data.py: downloads CSVs into data_raw/ via yfinance
make schema              # sql/00_schema.sql: creates raw/staging/analytics schemas + tables
make load_raw             # truncates raw.* and \copy's the three CSVs in
make validate              # sql/10_validations.sql: fail-fast checks on raw data
make staging                 # sql/20_staging_transform.sql: raw -> staging, truncates staging+analytics first
make features                  # sql/30_analytics_features.sql: computes analytics.features_daily
make labels                       # sql/40_analytics_labels.sql: computes analytics.labels_daily
make training_dataset                # sql/50_training_dataset.sql: creates analytics.v_training_dataset view
```

There is no test suite, linter, or CI configured in this repo.

To inspect the warehouse directly:
```bash
psql -d edmp_engine
```

## Architecture

**Layered SQL warehouse.** Every table lives in one of three schemas, and data only ever flows forward:

```
raw.*        as-ingested CSV data (raw.assets, raw.prices_daily, raw.events)
  ↓  (sql/20_staging_transform.sql)
staging.*    cleaned/typed, symbol -> asset_id resolved, computed fields (surprise, surprise_pct)
  ↓  (sql/30_analytics_features.sql, sql/40_analytics_labels.sql)
analytics.*  features_daily, labels_daily, and the model/backtest tables (not yet populated)
```

**Numbered SQL files run in strict order** (`00_schema.sql` → `50_training_dataset.sql`); the number prefix
is the authoritative execution order and mirrors the Makefile dependency chain. Each transform/feature/label
script wraps its work in `BEGIN;`/`COMMIT;` and `TRUNCATE`s its own output table(s) at the top before
recomputing — the pipeline is designed to be re-run from scratch idempotently rather than incrementally
updated. `20_staging_transform.sql` truncates all of `staging.*` **and** `analytics.*` (in dependency order)
before rebuilding staging, since downstream analytics would otherwise be orphaned.

**Warehouse keys**: `raw.*` tables key on natural identifiers (`symbol`, `symbol+trading_date`). Once data
enters `staging.*`, everything re-keys on the surrogate `asset_id` (from `staging.assets`), and all
`analytics.*` tables key on `(asset_id, trading_date)`. When adding a new analytics table, follow this
convention rather than joining back through `symbol`.

**Point-in-time correctness is a hard constraint**, not just a convention (see "Time-Series Validation
Protocol" in README.md). `analytics.features_daily` must only use data at or before `trading_date` (built
with `LAG`/trailing window frames like `ROWS BETWEEN 19 PRECEDING AND CURRENT ROW`); `analytics.labels_daily`
is the only table allowed to look forward, using `LEAD(close)` to compute `ret_fwd_1d` for `trading_date`'s
*next*-day outcome. Any new feature must use trailing-only window frames; any new label must live in the
labels table, never mixed into features. `analytics.v_training_dataset` (sql/50_training_dataset.sql) is the
join point that strictly filters out rows with any NULL feature/label, so it's the dataset a future model
would actually train on.

**Event → asset mapping is a placeholder.** `staging.event_asset_map` currently does a `CROSS JOIN` of every
event to every asset with `weight = 1.0` (see `20_staging_transform.sql` step 4) — this is explicitly called
out in-file as a baseline to replace with real mapping logic later. `raw.events`/`staging.events` are wired
into the schema but `python/prepare_data.py` currently only writes an empty, schema-valid `events.csv`
(no real event ingestion yet).

**Data validation happens between raw load and staging transform** (`sql/10_validations.sql`, run via
`make validate`), using `RAISE EXCEPTION` inside `DO $$ ... $$` blocks to fail the whole `make run` fast
on bad input (nulls, negative prices, `high < low`, duplicate keys, future-dated rows) before it ever reaches
staging.

**Adding a new asset**: add an entry to the `ASSETS` list in `python/prepare_data.py`, then `make run`.

**Adding a new feature or label**: add the column to the relevant `CREATE TABLE` in `sql/00_schema.sql`,
then extend the corresponding numbered transform script's CTE chain, keeping the trailing-only /
forward-only discipline described above.
