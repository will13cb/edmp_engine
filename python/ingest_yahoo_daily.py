import os
import pandas as pd
import yfinance as yf

OUT_DIR = "data_raw"
TICKERS = ["SPY", "TLT", "XLE"]
START = "2018-01-01"
END = None      # None means "up to the latest available date" in yfinance.download()

# This function downloads daily price data for a single ticker and returns it as a DataFrame.
def _download_one(symbol: str) -> pd.DataFrame:
    # Return a DataFrame with columns like "Open", "High", "Low", "Close", "Adj Close", "Volume".
    df = yf.download(symbol, start=START, end=END, auto_adjust=False, progress=False)

    # If no data is returned, raise an error to alert the user.
    if df.empty:
        raise RuntimeError(f"No data returned for {symbol}")

    # If columns are MultiIndex (grouping data), keep only the price field names
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Reset the index to turn the "Date" index into a regular column in the df
    df = df.reset_index()

    df.rename(columns={
        "Date": "trading_date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",   # close price adjusted for dividends and splits
        "Volume": "volume",
    }, inplace=True)    # apply the changes to the original object

    # Add "symbol" and "source" columns to the DataFrame
    df["symbol"] = symbol
    df["source"] = "yahoo"

    # Reorder the columns to a consistent format
    df = df[["symbol","trading_date","open","high","low","close","adj_close","volume","source"]]

    # Convert the "trading_date" column to datetime.date format (remove time component)
    df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date
    return df

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # create assets.csv for each asset in TICKERS(1 row per asset, with metadata about the asset)
    assets = [
        ("SPY", "SPDR S&P 500 ETF", "etf", "USD", "NYSE"),
        ("TLT", "iShares 20+ Year Treasury Bond ETF", "etf", "USD", "NASDAQ"),
        ("XLE", "Energy Select Sector SPDR Fund", "etf", "USD", "NYSE"),
    ]
    assets_df = pd.DataFrame(assets, columns=["symbol","name","asset_type","currency","exchange"])
    assets_df["source"] = "yahoo"
    assets_df.to_csv(os.path.join(OUT_DIR, "assets.csv"), index=False)

    # prices_daily.csv (long format)
    all_prices = []
    for sym in TICKERS:
        all_prices.append(_download_one(sym))

    # Concatenate the list of DataFrames into a single DataFrame, 
    # ignoring the original index to create a new one.
    prices_df = pd.concat(all_prices, ignore_index=True)

    # Write the combined DataFrame to a CSV file without the index column.
    prices_df.to_csv(os.path.join(OUT_DIR, "prices_daily.csv"), index=False)

    print("Wrote:")
    print(f"  {OUT_DIR}/assets.csv")
    print(f"  {OUT_DIR}/prices_daily.csv")
    print(f"Rows in prices_daily.csv: {len(prices_df):,}")
    
# If this script is run directly (instead of imported as a module), execute the main() function.
if __name__ == "__main__":
    main()