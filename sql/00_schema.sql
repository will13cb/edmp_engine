-- ==========================================================
-- Event-Driven Market Probability Engine
-- PostgreSQL Warehouse Schema
-- Schemas: raw, staging, analytics
-- ==========================================================

BEGIN;

-- 1) Schemas
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS analytics;

-- If the object name is not schema-qualified, search in this order:
SET search_path = analytics, staging, raw, public;

-- ==========================================================
-- RAW LAYER (as ingested)
-- ==========================================================

CREATE TABLE IF NOT EXISTS raw.assets (
  symbol       text PRIMARY KEY,
  name         text,
  asset_type   text,
  currency     text,
  exchange     text,
  source       text,
  ingested_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.prices_daily (
  symbol        text NOT NULL,
  trading_date  date NOT NULL,
  open          double precision,
  high          double precision,
  low           double precision,
  close         double precision,
  adj_close     double precision,
  volume        double precision,
  source        text,
  ingested_at   timestamptz DEFAULT now(),
  -- raw de-dupe key (prevents duplicate rows for same symbol/date)
  CONSTRAINT prices_daily_raw_uniq UNIQUE (symbol, trading_date),

  -- ensure symbol exists in raw.assets
  CONSTRAINT prices_daily_raw_symbol_fk
    FOREIGN KEY (symbol) REFERENCES raw.assets(symbol)
);

-- most operations in the pipeline look up prices by asset and time. This index will speed it up.
CREATE INDEX IF NOT EXISTS ix_raw_prices_symbol_date
  ON raw.prices_daily(symbol, trading_date);

CREATE TABLE IF NOT EXISTS raw.events (
  event_id     bigserial PRIMARY KEY,
  event_type   text NOT NULL,
  event_ts     timestamptz NOT NULL,
  event_date   date,
  title        text,
  country      text,
  source       text,
  actual       double precision,
  forecast     double precision,
  previous     double precision,
  raw_text     text,
  ingested_at  timestamptz DEFAULT now()
);

-- useful for quickly finding events in a given time range
CREATE INDEX IF NOT EXISTS ix_raw_events_ts
  ON raw.events(event_ts);

-- ==========================================================
-- STAGING LAYER (cleaned / standardized)
-- ==========================================================

CREATE TABLE IF NOT EXISTS staging.assets (
  asset_id      bigserial PRIMARY KEY,
  symbol        text NOT NULL UNIQUE,
  name          text,
  asset_type    text,
  currency      text,
  exchange      text,
  active        boolean NOT NULL DEFAULT true,
  first_seen_at timestamptz DEFAULT now(),
  last_seen_at  timestamptz DEFAULT now()
);

-- Warehouse key is (asset_id, trading_date)
CREATE TABLE IF NOT EXISTS staging.prices_daily (
  asset_id      bigint NOT NULL,
  trading_date  date NOT NULL,
  open          double precision,
  high          double precision,
  low           double precision,
  close         double precision,
  adj_close     double precision,
  volume        double precision,
  source        text,
  loaded_at     timestamptz DEFAULT now(),

  -- de-dupe key (prevents duplicate rows for same asset_id/date)
  CONSTRAINT prices_daily_stg_pk PRIMARY KEY (asset_id, trading_date),

  -- ensure asset_id exists in staging.assets
  CONSTRAINT prices_daily_stg_asset_fk
    FOREIGN KEY (asset_id) REFERENCES staging.assets(asset_id),

  -- Ensure no negative prices or volumes (can be NULL if missing, but not negative)
  CONSTRAINT prices_daily_stg_nonneg_volume
    CHECK (volume IS NULL OR volume >= 0)
);



CREATE TABLE IF NOT EXISTS staging.events (
  event_id       bigserial PRIMARY KEY,
  event_type     text NOT NULL,
  event_ts       timestamptz NOT NULL,
  event_date     date NOT NULL,            -- must be set in staging
  title          text,
  country        text,
  source         text,
  actual         double precision,
  forecast       double precision,
  previous       double precision,
  surprise       double precision,         -- actual - forecast
  surprise_pct   double precision,         -- surprise / abs(forecast) (define in transform)
  cleaned_text   text,
  loaded_at      timestamptz DEFAULT now()
);

-- Index for quickly finding events by date and type
CREATE INDEX IF NOT EXISTS ix_stg_events_date_type
  ON staging.events(event_date, event_type);

-- map events to assets
CREATE TABLE IF NOT EXISTS staging.event_asset_map (
  event_id  bigint NOT NULL,
  asset_id  bigint NOT NULL,
  weight    double precision,
  CONSTRAINT event_asset_map_pk PRIMARY KEY (event_id, asset_id),
  CONSTRAINT event_asset_map_event_fk
    FOREIGN KEY (event_id) REFERENCES staging.events(event_id),
  CONSTRAINT event_asset_map_asset_fk
    FOREIGN KEY (asset_id) REFERENCES staging.assets(asset_id)
);

-- Index for quickly finding all assets related to an event
CREATE INDEX IF NOT EXISTS ix_event_asset_map_asset
  ON staging.event_asset_map(asset_id);

-- ==========================================================
-- ANALYTICS LAYER (features, labels, model outputs)
-- ==========================================================

CREATE TABLE IF NOT EXISTS analytics.features_daily (
  asset_id       bigint NOT NULL,
  trading_date   date NOT NULL,
  ret_1d         double precision,
  logret_1d      double precision,
  vol_20d        double precision,
  mom_5d         double precision,
  mom_20d        double precision,
  drawdown_60d   double precision,
  CONSTRAINT features_daily_pk PRIMARY KEY (asset_id, trading_date),
  CONSTRAINT features_daily_asset_fk
    FOREIGN KEY (asset_id) REFERENCES staging.assets(asset_id)
);

-- labels for next-day direction and large moves (e.g. >2% move in either direction)
CREATE TABLE IF NOT EXISTS analytics.labels_daily (
  asset_id             bigint NOT NULL,
  trading_date         date NOT NULL,
  ret_fwd_1d           double precision,
  abs_ret_fwd_1d       double precision,
  y_up_next_day        boolean,
  y_large_move_next    boolean,
  CONSTRAINT labels_daily_pk PRIMARY KEY (asset_id, trading_date),
  CONSTRAINT labels_daily_asset_fk
    FOREIGN KEY (asset_id) REFERENCES staging.assets(asset_id)
);

CREATE TABLE IF NOT EXISTS analytics.model_runs (
  model_run_id         bigserial PRIMARY KEY,
  model_name           text NOT NULL,
  feature_set_version  text NOT NULL,
  train_start          date NOT NULL,
  train_end            date NOT NULL,
  test_start           date,
  test_end             date,
  git_commit           text,
  created_at           timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analytics.model_predictions (
  model_run_id   bigint NOT NULL,
  asset_id       bigint NOT NULL,
  trading_date   date NOT NULL,
  p_up           double precision NOT NULL,
  p_large_move   double precision NOT NULL,
  signal         smallint,
  created_at     timestamptz DEFAULT now(),
  CONSTRAINT model_predictions_pk PRIMARY KEY (model_run_id, asset_id, trading_date),
  CONSTRAINT model_predictions_run_fk
    FOREIGN KEY (model_run_id) REFERENCES analytics.model_runs(model_run_id),
  CONSTRAINT model_predictions_asset_fk
    FOREIGN KEY (asset_id) REFERENCES staging.assets(asset_id),
  CONSTRAINT model_predictions_prob_bounds
    CHECK (p_up >= 0 AND p_up <= 1 AND p_large_move >= 0 AND p_large_move <= 1)
);

-- backtest_results predates analytics.backtest_runs and was keyed on
-- model_run_id with no strategy or cost columns. Every statement in this file is
-- CREATE TABLE IF NOT EXISTS, which is idempotent but does not migrate, so an
-- existing warehouse would silently keep the old shape and the Phase D writer
-- would fail against it.
--
-- Dropping is safe here only because nothing ever wrote to this table before
-- Phase D. That is checked rather than assumed: if rows are present, this raises
-- instead of destroying them. A schema file that can quietly delete data is a
-- worse trade than one that occasionally needs a manual decision.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'analytics' AND table_name = 'backtest_results'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'analytics' AND table_name = 'backtest_results'
      AND column_name = 'backtest_run_id'
  ) THEN
    IF (SELECT count(*) FROM analytics.backtest_results) > 0 THEN
      RAISE EXCEPTION
        'analytics.backtest_results holds rows in the pre-backtest_runs shape. '
        'Migrate or drop them deliberately; this file will not discard them.';
    END IF;
    DROP TABLE analytics.backtest_results;
  END IF;
END $$;

-- One row per (model run, position rule, cost assumption). Mirrors the
-- model_runs / model_predictions split, for the same reason: a result is not
-- interpretable without the assumptions that produced it.
--
-- Two things live here that backtest_results structurally cannot hold. First,
-- the summary metrics -- Sharpe, max drawdown, hit rate, expectancy are one
-- scalar per run, not a daily series, and they are the numbers actually being
-- compared. Second, the assumptions: a net_return means nothing without the cost
-- charged to produce it, so cost_bps is recorded rather than remembered.
--
-- strategy exists because the point of the backtest is comparison. The same
-- predictions must be runnable through a threshold rule and through an
-- always-long benchmark, and an "edge" is only ever the difference between them.
CREATE TABLE IF NOT EXISTS analytics.backtest_runs (
  backtest_run_id  bigserial PRIMARY KEY,
  model_run_id     bigint NOT NULL,
  strategy         text NOT NULL,
  cost_bps         double precision NOT NULL,
  n_days           integer,
  n_trades         integer,
  total_return     double precision,
  sharpe           double precision,
  max_drawdown     double precision,
  hit_rate         double precision,
  expectancy       double precision,
  git_commit       text,
  created_at       timestamptz DEFAULT now(),
  CONSTRAINT backtest_runs_model_fk
    FOREIGN KEY (model_run_id) REFERENCES analytics.model_runs(model_run_id),
  -- Sharpe is unbounded but a hit rate is a proportion; a violation means the
  -- metric was computed wrong, not that the strategy was unusual.
  CONSTRAINT backtest_runs_hit_rate_bounds
    CHECK (hit_rate IS NULL OR (hit_rate >= 0 AND hit_rate <= 1)),
  CONSTRAINT backtest_runs_drawdown_sign
    CHECK (max_drawdown IS NULL OR max_drawdown <= 0),
  CONSTRAINT backtest_runs_cost_nonneg
    CHECK (cost_bps >= 0)
);

-- The daily series behind one backtest_runs row.
--
-- turnover and avg_position are stored, not just the returns, so the cost
-- deduction is auditable rather than asserted: net_return must equal
-- gross_return - turnover * cost_bps/10000, and sql/90_assertions.sql re-derives
-- it. Storing only the returns would make an incorrect cost model invisible.
CREATE TABLE IF NOT EXISTS analytics.backtest_results (
  backtest_run_id   bigint NOT NULL,
  trading_date      date NOT NULL,
  avg_position      double precision,
  turnover          double precision,
  gross_return      double precision,
  net_return        double precision,
  cum_return        double precision,
  drawdown          double precision,
  hit_rate_rolling  double precision,
  sharpe_rolling    double precision,
  CONSTRAINT backtest_results_pk PRIMARY KEY (backtest_run_id, trading_date),
  CONSTRAINT backtest_results_run_fk
    FOREIGN KEY (backtest_run_id) REFERENCES analytics.backtest_runs(backtest_run_id),
  CONSTRAINT backtest_results_turnover_nonneg
    CHECK (turnover IS NULL OR turnover >= 0),
  CONSTRAINT backtest_results_drawdown_sign
    CHECK (drawdown IS NULL OR drawdown <= 0)
);

-- index for quickly retrieving backtest results by run and date
CREATE INDEX IF NOT EXISTS ix_backtest_run_date
  ON analytics.backtest_results(backtest_run_id, trading_date);

-- and for finding every backtest of a given model run
CREATE INDEX IF NOT EXISTS ix_backtest_runs_model
  ON analytics.backtest_runs(model_run_id, strategy);

COMMIT;