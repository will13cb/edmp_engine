# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EDMP Engine (Event-Driven Market Probability Engine) is a reproducible data pipeline plus a baseline
probabilistic model. It ingests daily ETF prices from Yahoo Finance, loads them into PostgreSQL through a
raw → staging → analytics layered warehouse, computes return/volatility/momentum features, generates
forward-looking labels, and trains a walk-forward-validated logistic regression whose predictions are written
back into the warehouse.

Status against the roadmap in README.md ("Implementation Roadmap"): Phases A–C are done (baseline model,
walk-forward validation with embargo, honest evaluation). `analytics.model_runs` and
`analytics.model_predictions` are populated by `python/train_baseline_logreg.py`.
Phase D is done: `python/backtest_from_predictions.py` writes `analytics.backtest_runs` /
`analytics.backtest_results`. **Events are not implemented**: `raw.events` /
`staging.events` are wired into the schema but no real event data is ingested yet (Phase E).

Three docs carry context that isn't derivable from the code: README.md "Implementation Roadmap" (what's done,
what's next, and the measured results of each phase), `docs/design_decisions.md` (**read this first** — the
rationale behind the architecture, validation discipline, and tests, plus known limitations and decisions
deliberately deferred), `docs/course_validation_and_backtesting.md` (the concepts behind Phases B–D,
written against this project's actual tables), and `docs/course_calibration_and_events.md` (the same
treatment for calibration and Phase E — probability calibration and where to fit it, effective
trading-date alignment, surprise vs level, the two point-in-time classes of event feature, and the
multiple-comparisons discipline).

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
make prepare_data       # python/prepare_data.py: downloads prices into data_raw/ via yfinance (concurrently)
make schema              # sql/00_schema.sql: creates raw/staging/analytics schemas + tables
make load_raw             # truncates raw.* and \copy's in config/assets.csv + the two data_raw/ CSVs
make validate              # sql/10_validations.sql: fail-fast checks on raw data
make staging                 # sql/20_staging_transform.sql: raw -> staging, truncates staging+analytics first
make features                  # sql/30_analytics_features.sql: computes analytics.features_daily
make labels                       # sql/40_analytics_labels.sql: computes analytics.labels_daily
make training_dataset                # sql/50_training_dataset.sql: creates analytics.v_training_dataset view
make train_baseline                   # python/train_baseline_logreg.py: walk-forward training + evaluation
make backtest                          # python/backtest_from_predictions.py: predictions -> returns, costs, risk metrics
```

`make run` stops at `training_dataset` and deliberately does **not** include `train_baseline`: `make run` is
the deterministic rebuild, whereas every training invocation appends new `model_runs`/`model_predictions`
rows (see "Modeling layer" below). Run training as an explicit separate step.

Tests:
```bash
make test                             # pytest (fast, no DB) then sql/90_assertions.sql (needs a built warehouse)
.venv/bin/python -m pytest tests/ -q  # layer 1 only, for a fast edit loop
```

There is no linter or CI configured in this repo.

**Working offline / avoiding re-downloads**: every stage from `load_raw` onward has `prepare_data` as a Make
prerequisite, and Make re-runs it every time (no file-staleness check), so `make train_baseline` will try to
hit Yahoo Finance even when `data_raw/*.csv` already exists. To re-run the SQL stages against already-
downloaded CSVs, invoke `psql -v ON_ERROR_STOP=1 -d edmp_engine -f sql/<file>.sql` directly instead of going
through Make.

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
analytics.*  features_daily, labels_daily
  ↓  (sql/50_training_dataset.sql)
analytics.v_training_dataset   join of features + labels, NULL-filtered
  ↓  (python/train_baseline_logreg.py)
analytics.model_runs, analytics.model_predictions
  ↓  (python/backtest_from_predictions.py)
analytics.backtest_runs, analytics.backtest_results
```

**Numbered SQL files run in strict order** (`00_schema.sql` → `50_training_dataset.sql`, with
`90_assertions.sql` run separately by `make test`); the number prefix
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
join point that strictly filters out rows with any NULL feature/label, so it's the dataset the model actually
trains on.

**Modeling layer** (`python/train_baseline_logreg.py`, run via `make train_baseline`). Conventions here differ
deliberately from the SQL layer's:

- **The SQL stages truncate-and-recompute; the modeling tables are append-only within a warehouse
  generation.** `model_runs` is an experiment log (bigserial PK, `created_at`, `git_commit`) — its purpose is
  to let multiple runs coexist and be compared. Since a fresh `model_run_id` is part of
  `model_predictions`'s composite PK, reruns can never collide, so `train_baseline_logreg.py` contains no
  `TRUNCATE`/`ON CONFLICT` logic and none should be added.
  **A rebuild does clear the log, and must**: `20_staging_transform.sql` truncates `model_predictions`,
  `backtest_results`, `backtest_runs` and `model_runs`, because `model_predictions.asset_id` references `staging.assets`,
  which is rebuilt `RESTART IDENTITY`. Surviving rows would point at renumbered ids — misattributed to the
  wrong instrument rather than merely stale. So the log accumulates across training runs and resets on
  `make run`. Do not "fix" this by removing those tables from the `TRUNCATE`; the real fix, deferred, is
  re-keying on `symbol` (see `docs/design_decisions.md` §2 and §11). `pg_dump` the two tables first if a
  particular run's output must outlive a rebuild.
- **Walk-forward validation, one `model_runs` row per fold.** Folds are expanding-window and distinguished
  purely by their `train_start`/`train_end`/`test_start`/`test_end` ranges — the schema needs no fold column.
- **Embargo is mandatory, not optional.** `EMBARGO_DAYS = 60` (matching the longest lookback,
  `drawdown_60d`) drops test rows near each cutoff whose trailing feature windows would otherwise be computed
  partly from training-period prices. Any change to the feature set's longest window must update this.
- **Only out-of-sample (test) rows are written to `model_predictions`.** The table has no train/test marker
  column, so writing in-sample rows would silently corrupt any downstream backtest that reads it as genuine
  forecasts.
- Scalers and models are refit **inside** each fold on that fold's training rows only.
- DB access is raw `psycopg` (v3) with `psycopg.connect(dbname="edmp_engine")` — no host/user/password, no
  ORM, no `.env`, matching the bare `psql -d edmp_engine` convention used everywhere else.

**Known model status** (measured on 15 ETFs / 32,295 rows; see `docs/design_decisions.md` §8): direction
(`y_up_next_day`) has no signal — ROC-AUC ~0.51 across folds, straddling 0.5 — and the naive
majority-class baseline beats the model on accuracy in every fold. `y_large_move_next` holds a real edge
(~0.57 mean). Probabilities are poorly calibrated in the 0.55–0.60 bucket, so `p_up` must not be used for
proportional position sizing until calibration is fixed. Treat a direction AUC meaningfully above ~0.55 as
a leakage suspect rather than a win — that heuristic has already caught one real ingestion bug.

**Testing exists to catch silent leakage**, which is the failure mode that matters here: a temporal-leakage
regression raises no error and changes no row count, it just quietly inflates the metrics. Two layers, run by
`make test`:

- `tests/test_ingestion.py` — pytest over the settled-session cutoff, no database or network. Asserts a
  still-forming bar never enters the warehouse and that two runs on the same day agree.
- `tests/test_evaluation.py` — pytest over the per-symbol AUC breakdown and `safe_auc`. The latter matters
  more than it looks: scikit-learn 1.9 returns `nan` rather than raising for a single-class block, and `nan`
  passes an `is not None` guard, so an undefined fold would silently turn the reported mean and std into
  `nan`. `safe_auc` normalises both signals to `None`.
- `tests/test_folds.py` — pytest over `generate_folds`, no database. Asserts train/test never overlap, the
  embargo gap is exactly `EMBARGO_DAYS` trading days, the window actually expands, and test blocks are
  disjoint. `tests/conftest.py` puts `python/` on `sys.path` (the scripts are flat files, not a package).
- `sql/90_assertions.sql` — post-pipeline invariants, needs a built warehouse. Where `sql/10_validations.sql`
  guards raw *input*, this guards computed *output* by re-deriving each value from its definition. The
  strongest check is `ret_fwd_1d(t) == ret_1d(t+1)`: both equal `close(t+1)/close(t) - 1`, so any off-by-one
  shift in a `LEAD`/`LAG` or an unpartitioned window frame makes them diverge. It also asserts at the database
  level that every `model_predictions` row falls inside its run's test window — turning the "out-of-sample
  rows only" convention into something structurally enforced rather than merely intended.

When adding an assertion, derive it from a **definition**, not from observed output — otherwise a pre-existing
bug gets enshrined as expected behaviour. And confirm a new assertion can actually fail (break the invariant
deliberately, watch it fire, revert); an assertion that passes unconditionally is worse than none, because it
licenses false confidence.

**Tooling in `.claude/`** (committed and shared; `settings.local.json` and `scheduled_tasks.lock` are
gitignored as personal state):

- `hooks/comment_reminder.sh` — a `PostToolUse` hook registered in `.claude/settings.json`, firing on
  `Edit|Write` and filtering itself to `.py`/`.sql`. It asks for the *reasoning* behind a change, especially
  the point-in-time argument. Advisory only: it always exits 0 and must never be able to fail an edit.
- `skills/leakage-audit/` — the audit procedure described above.
- `skills/doc-audit/` — verifies a change is actually *reflected* in README.md, CLAUDE.md and
  `docs/design_decisions.md`, rather than merely accompanied by some doc edit. It maps each kind of change
  (new feature, new assertion, changed measured numbers, resolved limitation, completed phase, renamed
  file) to the specific sections that go stale, and checks the claims against the warehouse instead of
  trusting the diff.

**After each significant pipeline change**, run `make run` → `make test`, then invoke the **`leakage-audit`
skill** (`.claude/skills/leakage-audit/`) on the diff, then the **`doc-audit` skill** before committing.

Docs here rot in one direction: a change lands and the sections describing measured results, limitations,
phase status or the build log keep describing the system as it was. That is why `doc-audit` is a skill and
deliberately **not** a hook. A hook could only check that some `.md` file was touched — a proxy satisfied by
a blank line, unable to tell a results table carrying current fold numbers from one still describing a
three-asset warehouse. By this project's own standard (a check that passes unconditionally is worse than
none, because it manufactures confidence) that hook would be a bad assertion guarding the very thing meant
to keep the project honest. A hook can *prompt*; it cannot *verify*. See `docs/design_decisions.md` §11.

The two are complements, not substitutes. `make test` is the *ratchet*: it enforces the invariants someone has
already written down, mechanically and without fail. The audit skill is the *frontier*: it catches changes no
assertion covers yet — a newly added feature using `LEAD`, a scaler fit outside the fold loop (which leaves no
trace in the database at all), a feature that switches from a price ratio to a price level.

When the audit finds something, **convert it into an assertion** so the ratchet absorbs it and the audit never
has to catch that class again. `test_embargo_covers_the_longest_feature_lookback` is an example: it started as
a judgment call ("did anyone raise `EMBARGO_DAYS` after adding a longer window?") and is now a test that fails
by itself.

**Event → asset mapping is a placeholder.** `staging.event_asset_map` currently does a `CROSS JOIN` of every
event to every asset with `weight = 1.0` (see `20_staging_transform.sql` step 4) — this is explicitly called
out in-file as a baseline to replace with real mapping logic later. `raw.events`/`staging.events` are wired
into the schema but `python/prepare_data.py` currently only writes an empty, schema-valid `events.csv`
(no real event ingestion yet).

**Data validation happens between raw load and staging transform** (`sql/10_validations.sql`, run via
`make validate`), using `RAISE EXCEPTION` inside `DO $$ ... $$` blocks to fail the whole `make run` fast
on bad input (nulls, negative prices, `high < low`, duplicate keys, future-dated rows) before it ever reaches
staging. Two of the blocks guard things the rest of the suite structurally cannot see, because every other
check tests a property *of* price rows: **every configured asset must have at least one price row**
(a per-ticker fetch failure is caught in `prepare_data.py` so it can't abort the batch, which also makes it
silent), and **no two symbols may share an identical price series** (concurrent-fetch bugs have handed every
ticker the same frame; each duplicated series is internally consistent, so all of `90_assertions.sql` passes).
When adding a check, ask whether it would catch a row that is *absent* or *duplicated from another asset* —
those need their own assertions.

**Adding a new asset**: add a row to `config/assets.csv`, then `make run`. That file is the tracked
source of truth for the universe — `python/prepare_data.py` reads it to decide which tickers to
fetch, and `make load_raw` `\copy`s it straight into `raw.assets`. It is deliberately *not* in
`data_raw/`: unlike `prices_daily.csv`/`events.csv` nothing is fetched to produce it, so there is no
as-ingested snapshot to regenerate and no reason for it to be gitignored.

**Adding a new feature or label**: add the column to the relevant `CREATE TABLE` in `sql/00_schema.sql`,
then extend the corresponding numbered transform script's CTE chain, keeping the trailing-only /
forward-only discipline described above. Then add it to `sql/50_training_dataset.sql` (both the SELECT list
and its NULL filter) and to `FEATURE_COLUMNS` in `python/train_baseline_logreg.py`. If the new feature's
lookback window is longer than 60 days, raise `EMBARGO_DAYS` to match it.
