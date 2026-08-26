-- Fail-fast validations on raw inputs.
-- Assumes raw.assets, raw.prices_daily, raw.events already loaded.

-- 1) raw.assets: symbol must exist and be unique-ish
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM raw.assets WHERE symbol IS NULL OR btrim(symbol) = '') THEN
    RAISE EXCEPTION 'Validation failed: raw.assets has NULL/blank symbol';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM raw.assets
    GROUP BY symbol
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.assets has duplicate symbol values';
  END IF;
END $$;

-- 2) raw.prices_daily: required fields + sane ranges + duplicates
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM raw.prices_daily
    WHERE symbol IS NULL OR btrim(symbol) = ''
       OR trading_date IS NULL
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.prices_daily has NULL symbol or trading_date';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM raw.prices_daily
    WHERE open  < 0 OR high < 0 OR low < 0 OR close < 0
       OR (adj_close IS NOT NULL AND adj_close < 0)
       OR (volume IS NOT NULL AND volume < 0)
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.prices_daily has negative price/volume';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM raw.prices_daily
    WHERE high < low
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.prices_daily has high < low';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM raw.prices_daily
    GROUP BY symbol, trading_date
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.prices_daily has duplicate (symbol, trading_date)';
  END IF;

  -- Optional: reject dates far in the future
  IF EXISTS (
    SELECT 1
    FROM raw.prices_daily
    WHERE trading_date > CURRENT_DATE + INTERVAL '3 days'
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.prices_daily has trading_date in the future';
  END IF;
END $$;

-- 2b) Distinct instruments must have distinct price series.
--
-- Two different tickers cannot have byte-identical OHLCV histories. If they do,
-- the ingestion layer handed the same data to several symbols.
--
-- This guards a failure that every other check in this project is blind to:
-- concurrent fetching once returned one ticker's frame for all of them, and
-- because each series was still internally consistent, all six assertions in
-- sql/90_assertions.sql passed (ret_fwd_1d(t) == ret_1d(t+1) holds fine within a
-- duplicated series), row counts were right, and the model reported a plausible
-- but meaningless AUC. Every existing check validates WITHIN one asset; this is
-- the only one that looks ACROSS them.
DO $$
DECLARE
  offenders text;
BEGIN
  -- Fingerprint each symbol's whole series. Cheaper than pairwise comparison and
  -- exact: any difference in any bar changes the hash.
  SELECT string_agg(symbols, '; ')
    INTO offenders
  FROM (
    SELECT string_agg(symbol, ', ' ORDER BY symbol) AS symbols
    FROM (
      SELECT
        symbol,
        md5(string_agg(
          coalesce(close::text, '') || '|' || coalesce(volume::text, ''),
          ',' ORDER BY trading_date
        )) AS series_hash
      FROM raw.prices_daily
      GROUP BY symbol
    ) fingerprints
    GROUP BY series_hash
    HAVING COUNT(*) > 1
  ) dupes;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'Validation failed: identical price series across distinct symbols (%). '
      'The ingestion layer served one ticker''s data to several symbols - suspect '
      'shared state in the fetch path, not a market coincidence.', offenders;
  END IF;
END $$;

-- 3) raw.events: basic checks (tune per your dataset)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM raw.events
    WHERE event_type IS NULL OR btrim(event_type) = ''
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.events has NULL/blank event_type';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM raw.events
    WHERE event_ts IS NULL AND event_date IS NULL
  ) THEN
    RAISE EXCEPTION 'Validation failed: raw.events has neither event_ts nor event_date';
  END IF;
END $$;