"""Pull daily adjusted-close data for the gate universes and cache as CSV.

Uses yfinance (no API key). For the crypto event we use yfinance's BTC-USD /
ETH-USD tickers — yfinance has BTC going back to 2014, sufficient for the
Terra/Luna reference period (2021-04 → 2022-04).

Output:
    gate/data/core8_prices.csv      # wide: rows=date, cols=ticker, val=adj close
    gate/data/core8_returns.csv     # log returns (first row NaN, dropped)
    gate/data/crypto8_prices.csv    # 8-asset universe used for Terra/Luna event
    gate/data/crypto8_returns.csv

    python -m gate.data_pull
"""
import pandas as pd
import numpy as np
import yfinance as yf
from pathlib import Path


CORE8 = ["XLK", "XLF", "XLE", "XLV", "SPY", "TLT", "GLD", "HYG"]
CRYPTO8 = ["XLK", "XLF", "SPY", "TLT", "GLD", "HYG", "BTC-USD", "ETH-USD"]

START = "2019-01-02"
END = "2023-04-30"


def _fetch(tickers, start, end) -> pd.DataFrame:
    raw = yf.download(
        tickers=" ".join(tickers),
        start=start, end=end,
        auto_adjust=True,          # use adjusted close directly
        progress=False,
        group_by="ticker",
        threads=True,
    )
    # Normalize to a wide DataFrame of adj-close, one column per ticker
    if isinstance(raw.columns, pd.MultiIndex):
        closes = {}
        for t in tickers:
            if t in raw.columns.get_level_values(0):
                sub = raw[t]
                if "Close" in sub.columns:
                    closes[t] = sub["Close"]
        df = pd.DataFrame(closes)
    else:
        df = raw[["Close"]].rename(columns={"Close": tickers[0]})
    df = df.dropna(how="all").sort_index()
    missing = [t for t in tickers if t not in df.columns]
    if missing:
        print(f"  warning: missing columns {missing}")
    return df


def _log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return np.log(prices / prices.shift(1)).dropna(how="all")


def main() -> None:
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, tickers in [("core8", CORE8), ("crypto8", CRYPTO8)]:
        print(f"[{name}] fetching {len(tickers)} tickers from {START} to {END} …")
        prices = _fetch(tickers, START, END)
        prices = prices.reindex(columns=tickers)
        # Forward-fill single-day NaNs (crypto vs equity calendar alignment)
        prices = prices.ffill(limit=2).dropna()
        returns = _log_returns(prices)
        p_out = out_dir / f"{name}_prices.csv"
        r_out = out_dir / f"{name}_returns.csv"
        prices.to_csv(p_out)
        returns.to_csv(r_out)
        print(f"  wrote {p_out.name}  ({len(prices)} rows × {len(prices.columns)} cols)")
        print(f"  wrote {r_out.name}  ({len(returns)} rows × {len(returns.columns)} cols)")
        print(f"  date range: {prices.index.min().date()} → {prices.index.max().date()}")
        print()


if __name__ == "__main__":
    main()
