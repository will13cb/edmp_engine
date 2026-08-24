BEGIN;

TRUNCATE analytics.features_daily;

-- Extract price data and compute the previous day's close
-- using a window function (LAG)
-- adj_close, not close: raw close carries split discontinuities (e.g. AAPL's
-- 4:1 in 2020) that would show up as a fake +/-75% return. adj_close retroactively
-- rescales pre-split prices, which is safe here only because every feature below
-- is a ratio of two adj_close values on the same series -- a uniform rescaling
-- cancels. This stops being safe the moment a feature uses a raw price level.
WITH base AS (
  SELECT
    asset_id,
    trading_date,
    adj_close,
    -- LAG(adj_close) returns the previous row's adjusted close price
    -- PARTITION BY asset_id ensures each asset is treated separately
    -- ORDER BY trading_date ensures the lag follows time order
    LAG(adj_close) OVER (
      PARTITION BY asset_id
      ORDER BY trading_date
      ) AS prev_close
  FROM staging.prices_daily
),

-- Compute daily simple returns and log returns
rets AS (
  SELECT
    asset_id,
    trading_date,
    adj_close,

    -- Simple daily return:
    -- (today_close / yesterday_close) - 1
    CASE
      WHEN prev_close IS NULL OR prev_close = 0 THEN NULL
      ELSE (adj_close / prev_close - 1.0)
    END AS ret_1d,

    -- Log return:
    -- ln(close / prev_close)
    CASE
      WHEN prev_close IS NULL OR prev_close <= 0 OR adj_close <= 0 THEN NULL
      ELSE LN(adj_close / prev_close)
    END AS logret_1d
  FROM base
),

-- Create financial indicators using window functions
feat AS (
  SELECT
    asset_id,
    trading_date,
    ret_1d,
    logret_1d,

    -- --------------------------------------------------------
    -- Rolling volatility (20 trading days)
    -- --------------------------------------------------------
    -- STDDEV_SAMP = sample standard deviation
    -- Measures variability of returns
    -- Window = current day + previous 19 days
    -- Total = 20 days
    STDDEV_SAMP(logret_1d) OVER (
      PARTITION BY asset_id
      ORDER BY trading_date
      ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
    ) AS vol_20d,

    -- --------------------------------------------------------
    -- 5-day momentum
    -- --------------------------------------------------------
    -- Momentum measures price change over a time horizon
    -- Formula: close_today / close_5_days_ago - 1
    CASE
      WHEN LAG(adj_close, 5) OVER (PARTITION BY asset_id ORDER BY trading_date) IS NULL
        OR LAG(adj_close, 5) OVER (PARTITION BY asset_id ORDER BY trading_date) = 0
      THEN NULL
      ELSE adj_close / LAG(adj_close, 5) OVER (PARTITION BY asset_id ORDER BY trading_date) - 1.0
    END AS mom_5d,

    -- --------------------------------------------------------
    -- 20-day momentum
    -- --------------------------------------------------------
    -- Same idea as above but over a longer horizon
    -- Formula: close_today / close_20_days_ago - 1
    CASE
      WHEN LAG(adj_close, 20) OVER (PARTITION BY asset_id ORDER BY trading_date) IS NULL
        OR LAG(adj_close, 20) OVER (PARTITION BY asset_id ORDER BY trading_date) = 0
      THEN NULL
      ELSE adj_close / LAG(adj_close, 20) OVER (PARTITION BY asset_id ORDER BY trading_date) - 1.0
    END AS mom_20d,

    -- --------------------------------------------------------
    -- Drawdown over 60 days
    -- --------------------------------------------------------
    -- Drawdown measures how far the price is below
    -- the recent peak (maximum price).
    --
    -- Formula:
    -- current_price / max_price_last_60_days - 1
    CASE
      WHEN MAX(adj_close) OVER (
        PARTITION BY asset_id
        ORDER BY trading_date
        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
      ) = 0
      THEN NULL
      ELSE adj_close / MAX(adj_close) OVER (
        PARTITION BY asset_id
        ORDER BY trading_date
        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
      ) - 1.0
    END AS drawdown_60d

  FROM rets
)

-- Insert computed features into the analytics table
INSERT INTO analytics.features_daily (
  asset_id, trading_date, ret_1d, logret_1d, vol_20d, mom_5d, mom_20d, drawdown_60d
)

-- Select features from the final CTE
SELECT
  asset_id, trading_date, ret_1d, logret_1d, vol_20d, mom_5d, mom_20d, drawdown_60d
FROM feat
ORDER BY asset_id, trading_date;

COMMIT;