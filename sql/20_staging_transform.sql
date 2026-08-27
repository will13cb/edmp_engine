BEGIN;

-- Clear analytics and rebuild staging to stay deterministic.
--
-- The model tables are included on purpose, which looks like it contradicts the
-- append-only experiment log described in CLAUDE.md and design_decisions.md §2.
-- It does not: nothing here overwrites a run, but staging.assets is rebuilt
-- RESTART IDENTITY, so asset_id values are reassigned from scratch. A surviving
-- model_predictions row would keep an asset_id that now names a different
-- instrument -- misattributed rather than merely stale, and silently so. Losing
-- the log is the better half of that trade.
--
-- Do not remove these three from the TRUNCATE to preserve history. Either
-- pg_dump them first, or re-key model_predictions on symbol (deferred, §11).
TRUNCATE
  analytics.model_predictions,
  analytics.backtest_results,
  analytics.backtest_runs,
  analytics.model_runs,
  analytics.labels_daily,
  analytics.features_daily,
  staging.event_asset_map,
  staging.prices_daily,
  staging.events,
  staging.assets
RESTART IDENTITY;

-- 1) Assets: raw -> staging
--
-- ORDER BY symbol is load-bearing, not cosmetic. asset_id is a bigserial, so it
-- is assigned in whatever order rows arrive; without an ORDER BY that order is
-- formally undefined. It currently matches config/assets.csv line order only
-- because a freshly \copy'd heap happens to scan in insertion order, which is an
-- implementation detail that would drift after any UPDATE or VACUUM FULL.
-- Sorting makes asset_id a deterministic function of the symbol set, which is
-- what the "same inputs produce the same warehouse" claim in
-- design_decisions.md §2 requires.
--
-- Note what this does NOT provide: ids are still reassigned on every rebuild, so
-- adding or removing a symbol shifts the ids of others. Nothing may assume an
-- asset_id means the same instrument across rebuilds -- see the TRUNCATE comment
-- above.
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
FROM raw.assets a  -- a = alias for raw.assets
ORDER BY a.symbol;

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