from __future__ import annotations

import asyncio
from pathlib import Path
import pandas as pd
import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data_raw"
ASSETS_CONFIG = ROOT / "config" / "assets.csv"
START_DATE = "2018-01-01"
END_DATE = None  # None means up to latest available date from Yahoo Finance.

# Caps concurrent yfinance requests. yf.download is I/O-bound (waiting on the
# network per ticker), so downloads run concurrently via asyncio; this bounds
# how many are in flight at once to stay under Yahoo's informal rate limits.
MAX_CONCURRENT_DOWNLOADS = 20


def load_asset_universe() -> pd.DataFrame:
    # config/assets.csv is the tracked source of truth for which tickers to
    # fetch (see CLAUDE.md "Adding a new asset"). `make load_raw` \copy's that
    # same file into the raw.assets *table*, so this script only ever reads it -
    # it writes no assets file of its own.
    return pd.read_csv(ASSETS_CONFIG)


def ensure_data_dir() -> None:
    DATA_RAW.mkdir(parents=True, exist_ok=True)


def _fetch_history(symbol: str, start: str, end: str | None) -> pd.DataFrame:
    """Fetch one ticker's daily bars.

    Deliberately yf.Ticker().history() and NOT yf.download(): download() stages
    its results in module-level shared state (yfinance.shared._DFS) which it
    resets on entry, so two concurrent download() calls overwrite each other's
    accumulator and every caller can receive the same, wrong ticker's frame.
    That corruption is silent -- each series stays internally consistent, so
    every per-asset assertion still passes. Ticker().history() keeps its result
    on the instance, so it is safe to call from several threads at once.
    """
    return yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)


async def _download_one_price_history(
    symbol: str, start: str, end: str | None, semaphore: asyncio.Semaphore
) -> pd.DataFrame | None:
    async with semaphore:
        print(f"Downloading prices for {symbol}...")
        try:
            # The fetch is blocking (network + parsing); to_thread frees the
            # event loop so other tickers' downloads can run concurrently.
            df = await asyncio.to_thread(_fetch_history, symbol, start, end)
        except Exception as exc:
            # One ticker's network failure must not abort the whole batch.
            print(f"Warning: failed to download {symbol}: {exc}")
            return None

    if df.empty:
        print(f"Warning: no data returned for {symbol}")
        return None

    # If yfinance returns MultiIndex columns, keep only the price field names.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Reset index so Date becomes a regular column.
    df = df.reset_index()

    rename_map = {
        "Date": "trading_date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    }
    df = df.rename(columns=rename_map)

    required_cols = [
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol}: missing expected columns: {missing}")

    df = df[required_cols].copy()
    df["symbol"] = symbol
    df["source"] = "yfinance"

    # Keep date-only values in CSV and downstream SQL tables.
    df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date

    return df


async def _download_all_price_histories(
    symbols: list[str], start: str, end: str | None
) -> list[pd.DataFrame | None]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    async with asyncio.TaskGroup() as tg:
        # Tasks are created in symbol order but complete in whatever order the
        # network returns; results are gathered back in the original order
        # below since final output is sorted by (symbol, trading_date) anyway.
        tasks = [tg.create_task(_download_one_price_history(s, start, end, semaphore)) for s in symbols]
    return [t.result() for t in tasks]


def download_prices(symbols: list[str], start: str = START_DATE, end: str | None = END_DATE) -> pd.DataFrame:
    results = asyncio.run(_download_all_price_histories(symbols, start, end))
    frames = [df for df in results if df is not None]

    if not frames:
        raise RuntimeError("No price data downloaded.")

    prices = pd.concat(frames, ignore_index=True)
    prices = prices[
        [
            "symbol",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "source",
        ]
    ].sort_values(["symbol", "trading_date"])

    out = DATA_RAW / "prices_daily.csv"
    prices.to_csv(out, index=False)
    print(f"Wrote {out}")

    return prices


def write_events_csv() -> pd.DataFrame:
    # Reproducible empty schema-valid file for MVP.
    # You can later replace this with real macro event ingestion.
    columns = [
        "event_type",
        "event_ts",
        "event_date",
        "title",
        "country",
        "source",
        "actual",
        "forecast",
        "previous",
        "raw_text",
    ]
    events = pd.DataFrame(columns=columns)

    out = DATA_RAW / "events.csv"
    events.to_csv(out, index=False)
    print(f"Wrote {out}")

    return events


def main() -> None:
    ensure_data_dir()

    assets_df = load_asset_universe()
    symbols = assets_df["symbol"].tolist()

    download_prices(symbols=symbols, start=START_DATE, end=END_DATE)
    write_events_csv()

    print("\nData preparation complete.")
    print("Created:")
    print(f"  - {DATA_RAW / 'prices_daily.csv'}")
    print(f"  - {DATA_RAW / 'events.csv'}")


if __name__ == "__main__":
    main()
