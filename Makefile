SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

DB=edmp_engine
PSQL=psql -v ON_ERROR_STOP=1 -d $(DB)

DATA_DIR ?= $(CURDIR)/data_raw
CONFIG_DIR ?= $(CURDIR)/config

PYTHON=.venv/bin/python

# ------------------------
# Prepare raw CSVs
# ------------------------
prepare_data:
	$(PYTHON) python/prepare_data.py

# ------------------------
# Schema
# ------------------------
schema:
	$(PSQL) -f sql/00_schema.sql

# ------------------------
# Load raw CSVs
# ------------------------
# raw.assets loads from config/assets.csv (tracked, hand-edited ticker list),
# not data_raw/ -- unlike prices/events there is nothing fetched to cache, so
# there is no "as-ingested snapshot" to regenerate on every prepare_data run.
load_raw: prepare_data schema	# Ensure schema is created before loading data
	$(PSQL) -c "TRUNCATE raw.prices_daily, raw.assets, raw.events RESTART IDENTITY;"
	$(PSQL) -c "\copy raw.assets(symbol,name,asset_type,currency,exchange,source) FROM '$(CONFIG_DIR)/assets.csv' CSV HEADER"
	$(PSQL) -c "\copy raw.prices_daily(symbol,trading_date,open,high,low,close,adj_close,volume,source) FROM '$(DATA_DIR)/prices_daily.csv' CSV HEADER"
	$(PSQL) -c "\copy raw.events(event_type,event_ts,event_date,title,country,source,actual,forecast,previous,raw_text) FROM '$(DATA_DIR)/events.csv' CSV HEADER"

validate: load_raw
	$(PSQL) $(PSQLFLAGS) -f sql/10_validations.sql

# ------------------------
# Staging rebuild
# ------------------------
staging: validate
	$(PSQL) -f sql/20_staging_transform.sql

# ------------------------
# Feature computation
# ------------------------
features: staging
	$(PSQL) -f sql/30_analytics_features.sql

# ------------------------
# Label computation
# ------------------------
labels: features
	$(PSQL) -f sql/40_analytics_labels.sql

# ------------------------
# Training dataset view
# ------------------------
training_dataset: labels
	$(PSQL) -f sql/50_training_dataset.sql

# ------------------------
# Baseline logistic regression model
# ------------------------
train_baseline: training_dataset
	$(PYTHON) python/train_baseline_logreg.py

# ------------------------
# Backtest stored predictions
# ------------------------
# No prerequisite on train_baseline, deliberately. This reads whatever
# predictions are already in the warehouse, so re-running it with a different
# --cost-bps or strategy costs nothing and does not append another training run.
# It will fail loudly if model_predictions is empty, which is the correct
# response to "backtest before training" rather than silently training first.
backtest:
	$(PYTHON) python/backtest_from_predictions.py

# ------------------------
# Tests
# ------------------------
# Two layers with very different costs, so pytest runs first and fails fast:
#   1. pytest      - pure fold/embargo logic, no database, milliseconds
#   2. assertions  - post-pipeline invariants, needs a populated warehouse
#
# Layer 2 reads analytics.*, so this assumes `make run` has already been done.
# No prerequisite on `run` here: rebuilding the warehouse on every test call
# would make the fast layer-1 loop unusable.
test:
	$(PYTHON) -m pytest tests/ -q
	$(PSQL) -f sql/90_assertions.sql

# ------------------------
# Full deterministic rebuild
# ------------------------
run: training_dataset