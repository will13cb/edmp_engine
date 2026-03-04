BEGIN;

TRUNCATE raw.prices_daily, raw.assets, raw.events RESTART IDENTITY;

COMMIT;