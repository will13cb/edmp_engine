BEGIN;

TRUNCATE analytics.labels_daily;

-- Compute the next day's close using LEAD()
-- adj_close, matching sql/30_analytics_features.sql: raw close would inject a
-- fake return on split days. LEAD(adj_close) is the one place in the pipeline
-- allowed to look past t -- ret_fwd_1d(t) describes the outcome at t+1, which
-- is exactly what a label (not a feature) is for.
WITH px AS (
  SELECT
    asset_id,
    trading_date,
    adj_close,

    -- LEAD(adj_close) returns the next row's adjusted close price
    -- This gives us tomorrow's price relative to today
    -- Partition ensures each asset is treated separately
    LEAD(adj_close) OVER (
      PARTITION BY asset_id
      ORDER BY trading_date
      ) AS next_close
  FROM staging.prices_daily
),

-- Forward returns represent the future outcome
-- These become the prediction target for models
fwd AS (
  SELECT
    asset_id,
    trading_date,

    -- Forward 1-day return:
    -- (next_day_close / today_close) - 1
    -- This represents tomorrow's return
    CASE
      WHEN next_close IS NULL OR adj_close IS NULL OR adj_close = 0 THEN NULL
      ELSE (next_close / adj_close - 1.0)
    END AS ret_fwd_1d
  FROM px
),

-- Combine forward returns with previously computed features
joined AS (
  SELECT
    f.asset_id,
    f.trading_date,
    f.ret_fwd_1d,
    ABS(f.ret_fwd_1d) AS abs_ret_fwd_1d,
    (f.ret_fwd_1d > 0) AS y_up_next_day,
    fe.vol_20d
  FROM fwd f

  -- Join with features table to access volatility
  LEFT JOIN analytics.features_daily fe
    ON fe.asset_id = f.asset_id
   AND fe.trading_date = f.trading_date
)

INSERT INTO analytics.labels_daily (
  asset_id, trading_date,
  ret_fwd_1d, abs_ret_fwd_1d,
  y_up_next_day, y_large_move_next
)
SELECT
  asset_id,
  trading_date,
  ret_fwd_1d,
  abs_ret_fwd_1d,
  CASE WHEN ret_fwd_1d IS NULL THEN NULL ELSE y_up_next_day END AS y_up_next_day,

  -- large move tomorrow = absolute forward return > 2x today's volatility
  CASE
    WHEN ret_fwd_1d IS NULL OR vol_20d IS NULL THEN NULL
    ELSE (ABS(ret_fwd_1d) > (2.0 * vol_20d))
  END AS y_large_move_next
FROM joined
ORDER BY asset_id, trading_date;

COMMIT;