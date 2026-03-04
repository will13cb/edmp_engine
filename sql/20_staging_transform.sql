BEGIN;

-- Clear analytics and rebuild staging to stay deterministic
TRUNCATE
  analytics.model_predictions,
  analytics.backtest_results,
  analytics.model_runs,
  analytics.labels_daily,
  analytics.features_daily,
  staging.event_asset_map,
  staging.prices_daily,
  staging.events,
  staging.assets
RESTART IDENTITY;

-- 1) Assets: raw -> staging
INSERT INTO staging.assets (symbol, name, asset_type, currency, exchange, active, first_seen_at, last_seen_at)
SELECT
  a.symbol,
  a.name,
  COALESCE(a.asset_type, 'etf') AS asset_type,  -- returns the first non-NULL value or defaults to 'etf' if missing
  COALESCE(a.currency, 'USD')   AS currency,
  a.exchange,
  true                          AS active,
  now()                         AS first_seen_at,
  now()                         AS last_seen_at
FROM raw.assets a;  -- a = alias for raw.assets

-- 2) Prices: resolve symbol -> asset_id
INSERT INTO staging.prices_daily (
  asset_id, trading_date, open, high, low, close, adj_close, volume, source, loaded_at
)
SELECT
  sa.asset_id,
  p.trading_date,
  p.open, p.high, p.low, p.close, p.adj_close, p.volume,
  p.source,
  now() AS loaded_at
FROM raw.prices_daily p   -- p = alias for raw.prices_daily
JOIN staging.assets sa    -- sa = alias for staging.assets
  ON sa.symbol = p.symbol;

-- 3) Events: set computed fields (surprise, surprise_pct)
-- Note: raw.events.event_date may be null. If null, we derive from event_ts::date.
INSERT INTO staging.events (
  event_type, event_ts, event_date,
  title, country, source,
  actual, forecast, previous,
  surprise, surprise_pct,
  cleaned_text,
  loaded_at
)
SELECT
  e.event_type,
  e.event_ts,
  COALESCE(e.event_date, (e.event_ts AT TIME ZONE 'UTC')::date) AS event_date,
  e.title,
  e.country,
  e.source,
  e.actual,
  e.forecast,
  e.previous,
  CASE
    WHEN e.actual IS NOT NULL AND e.forecast IS NOT NULL
      THEN (e.actual - e.forecast)
    ELSE NULL
  END AS surprise,
  CASE
    WHEN e.actual IS NOT NULL AND e.forecast IS NOT NULL AND ABS(e.forecast) > 0
      THEN (e.actual - e.forecast) / ABS(e.forecast)
    ELSE NULL
  END AS surprise_pct,
  NULL::text AS cleaned_text,
  now() AS loaded_at
FROM raw.events e;    -- e = alias for raw.events

-- 4) Event → Asset mapping (simple default)
-- For now: map every event to every asset (weight=1.0).
-- Implement later: replace with real mapping rules.
INSERT INTO staging.event_asset_map (event_id, asset_id, weight)
SELECT
  se.event_id,
  sa.asset_id,
  1.0 AS weight
FROM staging.events se  -- se = alias for staging.events
CROSS JOIN staging.assets sa; -- CROSS JOIN creates Cartesian product (every event with every asset)

COMMIT;