-- Post-pipeline invariant assertions.
--
-- Unlike sql/10_validations.sql (which guards raw INPUT before it reaches staging),
-- this file guards computed OUTPUT: it re-derives each feature/label from its own
-- definition and fails if the pipeline disagrees with itself.
--
-- Why this exists: a temporal-leakage regression here is silent. Nothing crashes,
-- no row count changes, the model just quietly trains on information it should not
-- have and reports an ROC-AUC that is a lie. These checks turn the project's
-- point-in-time conventions into failures you cannot miss.
--
-- Run after the pipeline has been built: make run && make test
-- (make test runs the pytest suite first, then this file.)
-- Assumes analytics.features_daily, analytics.labels_daily and
-- analytics.v_training_dataset are populated.

-- 1) THE key temporal invariant: ret_fwd_1d(t) must equal ret_1d(t+1).
--
-- Both are, by definition, adj/close(t+1)/close(t) - 1. The label reaches forward
-- from t; the feature looks back from t+1; they must describe the same move. If
-- either side is shifted by even one row (a LEAD(close,2), a mis-ordered window,
-- a partition that spans assets) these two series diverge and this fires.
--
-- This is the single most valuable assertion in the file: it catches the exact
-- class of bug that would otherwise inflate every metric downstream.
DO $$
DECLARE
  mismatches bigint;
  worst      double precision;
BEGIN
  WITH joined AS (
    SELECT
      l.asset_id,
      l.trading_date,
      l.ret_fwd_1d,
      -- The next row's realized return, per asset, in date order.
      LEAD(f.ret_1d) OVER (PARTITION BY f.asset_id ORDER BY f.trading_date) AS next_ret_1d
    FROM analytics.labels_daily l
    JOIN analytics.features_daily f
      ON f.asset_id = l.asset_id
     AND f.trading_date = l.trading_date
  )
  SELECT COUNT(*), COALESCE(MAX(ABS(ret_fwd_1d - next_ret_1d)), 0)
    INTO mismatches, worst
  FROM joined
  WHERE ret_fwd_1d IS NOT NULL
    AND next_ret_1d IS NOT NULL
    -- Float tolerance: these are computed by different expressions over the same
    -- prices, so bit-identical equality is not guaranteed.
    AND ABS(ret_fwd_1d - next_ret_1d) > 1e-9;

  IF mismatches > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: ret_fwd_1d(t) <> ret_1d(t+1) on % row(s), max diff %. '
      'Features and labels disagree about which day is "next" - suspect a shifted '
      'LEAD/LAG or a window frame that is not partitioned by asset_id.',
      mismatches, worst;
  END IF;
END $$;

-- 2) Labels must match their stated definitions.
--
-- These are cheap, but they are what stops a redefinition (say, changing the
-- large-move threshold) from silently landing while the README still documents
-- the old rule.
DO $$
DECLARE
  bad_up    bigint;
  bad_large bigint;
BEGIN
  SELECT COUNT(*) INTO bad_up
  FROM analytics.labels_daily
  WHERE ret_fwd_1d IS NOT NULL
    AND y_up_next_day IS DISTINCT FROM (ret_fwd_1d > 0);

  IF bad_up > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: y_up_next_day <> (ret_fwd_1d > 0) on % row(s)', bad_up;
  END IF;

  -- y_large_move_next is volatility-scaled per asset: |ret_fwd_1d| > 2 * vol_20d.
  -- vol_20d lives in features_daily, so this also confirms the two tables stay
  -- aligned on (asset_id, trading_date).
  SELECT COUNT(*) INTO bad_large
  FROM analytics.labels_daily l
  JOIN analytics.features_daily f
    ON f.asset_id = l.asset_id
   AND f.trading_date = l.trading_date
  WHERE l.ret_fwd_1d IS NOT NULL
    AND f.vol_20d IS NOT NULL
    AND l.y_large_move_next IS DISTINCT FROM (ABS(l.ret_fwd_1d) > 2.0 * f.vol_20d);

  IF bad_large > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: y_large_move_next <> (ABS(ret_fwd_1d) > 2 * vol_20d) on % row(s)',
      bad_large;
  END IF;
END $$;

-- 3) Sign sanity on the rolling features.
--
-- vol_20d is a standard deviation and drawdown_60d is a decline from a trailing
-- maximum, so their signs are structurally fixed. A violation means the window
-- frame or the arithmetic is wrong, not that the market did something unusual.
DO $$
DECLARE
  bad_vol bigint;
  bad_dd  bigint;
BEGIN
  SELECT COUNT(*) INTO bad_vol
  FROM analytics.features_daily
  WHERE vol_20d IS NOT NULL AND vol_20d < 0;

  IF bad_vol > 0 THEN
    RAISE EXCEPTION 'Assertion failed: vol_20d is negative on % row(s)', bad_vol;
  END IF;

  -- Current price over trailing max, minus 1. Cannot exceed 0 because the current
  -- row is inside its own window (ROWS BETWEEN 59 PRECEDING AND CURRENT ROW).
  -- A positive value would mean the window excludes the current row.
  SELECT COUNT(*) INTO bad_dd
  FROM analytics.features_daily
  WHERE drawdown_60d IS NOT NULL AND drawdown_60d > 1e-9;

  IF bad_dd > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: drawdown_60d is positive on % row(s) - the trailing-max '
      'window may not include the current row', bad_dd;
  END IF;
END $$;

-- 4) The training view must be free of NULLs.
--
-- The view already filters these, so this asserts the filter still covers every
-- column it selects. Adding a column to the SELECT list without adding it to the
-- WHERE clause is an easy mistake, and it would hand NaNs straight to sklearn.
DO $$
DECLARE
  nulls bigint;
BEGIN
  SELECT COUNT(*) INTO nulls
  FROM analytics.v_training_dataset
  WHERE ret_1d IS NULL
     OR logret_1d IS NULL
     OR vol_20d IS NULL
     OR mom_5d IS NULL
     OR mom_20d IS NULL
     OR drawdown_60d IS NULL
     OR ret_fwd_1d IS NULL
     OR abs_ret_fwd_1d IS NULL
     OR y_up_next_day IS NULL
     OR y_large_move_next IS NULL;

  IF nulls > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: v_training_dataset has % row(s) with a NULL feature/label', nulls;
  END IF;
END $$;

-- 5) Warm-up and the final row must be excluded, per asset.
--
-- mom_20d needs 21 rows, so the first 20 trading rows of every asset are unusable;
-- LEAD() leaves the last row without a forward return. Expected training rows per
-- asset is therefore (price rows - 21).
--
-- This guards against a subtler failure than it looks: if a window frame lost its
-- PARTITION BY asset_id, later assets would inherit warm-up from earlier ones and
-- gain rows they should not have.
DO $$
DECLARE
  offenders text;
BEGIN
  SELECT string_agg(format('%s (expected %s, got %s)', symbol, expected, actual), ', ')
    INTO offenders
  FROM (
    SELECT
      a.symbol,
      GREATEST(COUNT(p.trading_date) - 21, 0) AS expected,
      COALESCE(t.actual, 0)                   AS actual
    FROM staging.assets a
    JOIN staging.prices_daily p
      ON p.asset_id = a.asset_id
    LEFT JOIN (
      SELECT asset_id, COUNT(*) AS actual
      FROM analytics.v_training_dataset
      GROUP BY asset_id
    ) t ON t.asset_id = a.asset_id
    GROUP BY a.symbol, t.actual
  ) s
  WHERE expected <> actual;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'Assertion failed: v_training_dataset row count <> (price rows - 21) for: %',
      offenders;
  END IF;
END $$;

-- 6) Predictions must be strictly out-of-sample.
--
-- python/train_baseline_logreg.py writes only test-split rows, but that is a
-- convention enforced in Python. model_predictions has no train/test marker
-- column, so nothing structural stops in-sample rows being written - and if they
-- were, a Phase D backtest would read fitted values as if they were genuine
-- forecasts and report an edge that does not exist.
--
-- This asserts the rule at the database level, where it cannot be bypassed.
DO $$
DECLARE
  leaked bigint;
BEGIN
  SELECT COUNT(*) INTO leaked
  FROM analytics.model_predictions p
  JOIN analytics.model_runs r USING (model_run_id)
  WHERE p.trading_date <= r.train_end
     OR (r.test_start IS NOT NULL AND p.trading_date < r.test_start)
     OR (r.test_end   IS NOT NULL AND p.trading_date > r.test_end);

  IF leaked > 0 THEN
    RAISE EXCEPTION
      'Assertion failed: % prediction row(s) fall outside their run''s test window '
      '(in-sample or out-of-range predictions were written)', leaked;
  END IF;
END $$;

-- All assertions passed. Emitted so a successful run is visibly distinguishable
-- from a run that silently executed nothing.
DO $$
BEGIN
  RAISE NOTICE 'All post-pipeline assertions passed.';
END $$;
