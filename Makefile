SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

DB=edmp_engine
PSQL=psql -v ON_ERROR_STOP=1 -d $(DB)

DATA_DIR ?= $(CURDIR)/data_raw

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
load_raw: prepare_data schema	# Ensure schema is created before loading data
	$(PSQL) -c "TRUNCATE raw.prices_daily, raw.assets, raw.events RESTART IDENTITY;"
	$(PSQL) -c "\copy raw.assets(symbol,name,asset_type,currency,exchange,source) FROM '$(DATA_DIR)/assets.csv' CSV HEADER"
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
# Full deterministic rebuild
# ------------------------
run: training_dataset