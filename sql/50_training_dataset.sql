BEGIN;

-- Model-ready supervised dataset at t using features at t and labels for t+1 outcome.
-- Strict null filtering keeps only rows usable for baseline classifiers.
CREATE OR REPLACE VIEW analytics.v_training_dataset AS
SELECT
  f.asset_id,
  a.symbol,
  f.trading_date,
  f.ret_1d,
  f.logret_1d,
  f.vol_20d,
  f.mom_5d,
  f.mom_20d,
  f.drawdown_60d,
  l.ret_fwd_1d,
  l.abs_ret_fwd_1d,
  l.y_up_next_day,
  l.y_large_move_next
FROM analytics.features_daily f
JOIN analytics.labels_daily l
  ON l.asset_id = f.asset_id
 AND l.trading_date = f.trading_date
JOIN staging.assets a
  ON a.asset_id = f.asset_id
WHERE f.ret_1d IS NOT NULL
  AND f.logret_1d IS NOT NULL
  AND f.vol_20d IS NOT NULL
  AND f.mom_5d IS NOT NULL
  AND f.mom_20d IS NOT NULL
  AND f.drawdown_60d IS NOT NULL
  AND l.ret_fwd_1d IS NOT NULL
  AND l.abs_ret_fwd_1d IS NOT NULL
  AND l.y_up_next_day IS NOT NULL
  AND l.y_large_move_next IS NOT NULL;

COMMIT;
