"""Invariants for what ingestion is allowed to put in the warehouse.

The pipeline's central promise is that a rebuild is deterministic: same inputs,
same warehouse. Yahoo serves the current session as a normal bar while it is
still forming, so ingesting it silently breaks that promise -- two rebuilds an
hour apart disagree -- and corrupts one label per asset, since ret_fwd_1d on the
second-to-last row would be measured against a close that has not happened yet.

These tests are pure: no database, no network.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from prepare_data import MARKET_TZ, drop_unsettled_bars, last_settled_session


UTC = ZoneInfo("UTC")


def frame(*days: int) -> pd.DataFrame:
    """Bars for the given days of 2026-08, in the shape download_prices builds."""
    return pd.DataFrame(
        {"symbol": ["SPY"] * len(days), "trading_date": [date(2026, 8, d) for d in days]}
    )


def test_todays_forming_session_is_dropped():
    """The bug this file exists for: a mid-session bar must never be ingested."""
    midday = datetime(2026, 8, 26, 13, 8, tzinfo=MARKET_TZ)
    kept = drop_unsettled_bars(frame(24, 25, 26), midday)
    assert kept["trading_date"].tolist() == [date(2026, 8, 24), date(2026, 8, 25)]


def test_settled_sessions_are_kept():
    kept = drop_unsettled_bars(frame(24, 25), datetime(2026, 8, 26, 13, 8, tzinfo=MARKET_TZ))
    assert len(kept) == 2


def test_two_runs_on_the_same_day_agree():
    """The actual invariant. Before the fix, a 09:30 run and a 15:59 run ingested
    different versions of the same bar, so `make run` was not reproducible."""
    bars = frame(24, 25, 26)
    at_open = drop_unsettled_bars(bars, datetime(2026, 8, 26, 9, 30, tzinfo=MARKET_TZ))
    near_close = drop_unsettled_bars(bars, datetime(2026, 8, 26, 15, 59, tzinfo=MARKET_TZ))
    after_close = drop_unsettled_bars(bars, datetime(2026, 8, 26, 20, 0, tzinfo=MARKET_TZ))

    assert at_open["trading_date"].tolist() == near_close["trading_date"].tolist()
    assert at_open["trading_date"].tolist() == after_close["trading_date"].tolist()


def test_cutoff_follows_market_time_not_machine_time():
    """01:00 UTC is still the previous afternoon in New York. A machine running in
    UTC must not decide the session boundary using its own local date, or the
    warehouse would depend on where it was built."""
    utc_after_midnight = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
    assert last_settled_session(utc_after_midnight) == date(2026, 8, 26)

    kept = drop_unsettled_bars(frame(25, 26), utc_after_midnight)
    assert kept["trading_date"].tolist() == [date(2026, 8, 25)]


def test_returns_a_copy_not_a_view():
    """Callers keep writing to the result (adding columns, sorting); a slice view
    would raise SettingWithCopyWarning or silently not stick."""
    kept = drop_unsettled_bars(frame(24, 25), datetime(2026, 8, 26, 12, 0, tzinfo=MARKET_TZ))
    kept["source"] = "yfinance"
    assert kept["source"].tolist() == ["yfinance", "yfinance"]
