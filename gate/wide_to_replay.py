"""Convert wide-format daily price CSV (date x tickers) to replay-compatible
long format (ts_ns, symbol, price) for feeding into app.backtest / app.live.

    python -m gate.wide_to_replay --input gate/data/crypto8_prices.csv \\
                                   --symbol BTC-USD \\
                                   --output gate/data/btc_replay.csv
"""
import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="wide-format CSV: Date index, tickers as cols")
    p.add_argument("--symbol", required=True, help="which column to extract")
    p.add_argument("--output", required=True, help="long-format CSV to write")
    args = p.parse_args()

    df = pd.read_csv(args.input, index_col=0, parse_dates=True)
    if args.symbol not in df.columns:
        raise SystemExit(f"column {args.symbol!r} not in {list(df.columns)}")
    series = df[args.symbol].dropna()
    out = pd.DataFrame({
        "ts_ns": series.index.astype("int64"),
        "symbol": args.symbol,
        "price": series.values,
    })
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)
    print(f"wrote {len(out):,} rows → {args.output}")
    print(f"range: {series.index.min().date()} → {series.index.max().date()}")


if __name__ == "__main__":
    main()
