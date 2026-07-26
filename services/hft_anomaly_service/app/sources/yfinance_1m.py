"""Fetch 1-minute bars from yfinance and write a replay-ready CSV.

NOTE: 1-minute bars are NOT tick data. yfinance intraday only goes back ~30
days and gives one OHLCV candle per minute. This source is a smoke-test for
the full pipeline (replay → detector → UI) with real market prices; it will
NOT validate microstructure-level anomaly detection. Use polygon_aggs or a
real tick source for that.

Usage:
    python -m app.sources.yfinance_1m AAPL --out /tmp/aapl.csv
    python -m app.sources.yfinance_1m AAPL MSFT SPY --period 5d --out /tmp/multi.csv
"""
import argparse
import csv
from pathlib import Path

import pandas as pd
import yfinance as yf


def fetch(tickers: list[str], period: str = "5d", interval: str = "1m") -> pd.DataFrame:
    data = yf.download(
        tickers=tickers,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    rows = []
    if len(tickers) == 1:
        sym = tickers[0]
        for ts, row in data.iterrows():
            close = row.get("Close")
            if pd.isna(close):
                continue
            rows.append((int(ts.value), sym, float(close)))
    else:
        for sym in tickers:
            if sym not in data.columns.get_level_values(0):
                continue
            sub = data[sym]
            for ts, row in sub.iterrows():
                close = row.get("Close")
                if pd.isna(close):
                    continue
                rows.append((int(ts.value), sym, float(close)))
    df = pd.DataFrame(rows, columns=["ts_ns", "symbol", "price"])
    return df.sort_values("ts_ns").reset_index(drop=True)


def write_csv(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ns", "symbol", "price"])
        for _, r in df.iterrows():
            w.writerow([int(r.ts_ns), r.symbol, f"{r.price:.6f}"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="+")
    p.add_argument("--period", default="5d", help="yfinance period (1d,5d,1mo,...)")
    p.add_argument("--interval", default="1m", help="yfinance interval (1m,2m,5m,...)")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    df = fetch(args.tickers, period=args.period, interval=args.interval)
    if df.empty:
        raise SystemExit("No data returned. Check tickers/period (yfinance 1m limit ≈ 30 days).")
    write_csv(df, args.out)
    print(f"wrote {len(df)} rows to {args.out}  "
          f"(symbols={sorted(df.symbol.unique().tolist())}, "
          f"range={pd.Timestamp(df.ts_ns.min()).isoformat()} → "
          f"{pd.Timestamp(df.ts_ns.max()).isoformat()})")


if __name__ == "__main__":
    main()
