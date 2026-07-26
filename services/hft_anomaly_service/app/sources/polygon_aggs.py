"""Polygon historical trades → replay-ready CSV.

Stub. Requires POLYGON_API_KEY and a paid plan for trades endpoint.

Usage (once implemented):
    python -m app.sources.polygon_aggs AAPL --date 2024-01-02 --out /tmp/aapl_trades.csv

Endpoint reference:
    GET /v3/trades/{ticker}?timestamp.gte=...&timestamp.lte=...

Each trade record has `sip_timestamp` (ns since epoch) and `price`. Paginate
via `next_url`. Write rows as (ts_ns, symbol, price).
"""
import argparse
import os


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("ticker")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise SystemExit("POLYGON_API_KEY not set")

    raise NotImplementedError(
        "Polygon trades fetcher not yet implemented. "
        "When wired up: call /v3/trades/{ticker} with date window, paginate, "
        "and write ts_ns,symbol,price CSV."
    )


if __name__ == "__main__":
    main()
