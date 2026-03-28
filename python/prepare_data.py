from __future__ import annotations

from pathlib import Path
import pandas as pd
import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data_raw"
START_DATE = "2018-01-01"
END_DATE = None  # None means up to latest available date from Yahoo Finance.

ASSETS = [
    {
        "symbol": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "asset_type": "etf",
        "currency": "USD",
        "exchange": "NYSE Arca",
        "source": "yfinance",
    },
    {
        "symbol": "TLT",
        "name": "iShares 20+ Year Treasury Bond ETF",
        "asset_type": "etf",
        "currency": "USD",
        "exchange": "NASDAQ",
        "source": "yfinance",
    },
    {
        "symbol": "XLE",
        "name": "Energy Select Sector SPDR Fund",
        "asset_type": "etf",
        "currency": "USD",
        "exchange": "NYSE Arca",
        "source": "yfinance",
    },
]


def ensure_data_dir() -> None:
    DATA_RAW.mkdir(parents=True, exist_ok=True)


def write_assets_csv() -> pd.DataFrame:
    df = pd.DataFrame(ASSETS)
    out = DATA_RAW / "assets.csv"
    df.to_csv(out, index=False)
    print(f"Wrote {out}")
    return df


def download_prices(symbols: list[str], start: str = START_DATE, end: str | None = END_DATE) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    for symbol in symbols:
        print(f"Downloading prices for {symbol}...")
        # yfinance defaults to daily bars when interval is not provided.
        df = yf.download(symbol, start=start, end=end, auto_adjust=False, progress=False)

        if df.empty:
            print(f"Warning: no data returned for {symbol}")
            continue

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

        frames.append(df)

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

    assets_df = write_assets_csv()
    symbols = assets_df["symbol"].tolist()

    download_prices(symbols=symbols, start=START_DATE, end=END_DATE)
    write_events_csv()

    print("\nData preparation complete.")
    print("Created:")
    print(f"  - {DATA_RAW / 'assets.csv'}")
    print(f"  - {DATA_RAW / 'prices_daily.csv'}")
    print(f"  - {DATA_RAW / 'events.csv'}")


if __name__ == "__main__":
    main()