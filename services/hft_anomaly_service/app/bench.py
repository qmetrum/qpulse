"""End-to-end latency benchmark.

    python -m app.bench                         # default 200k ticks, 4 symbols
    python -m app.bench --ticks 1000000 --tps 0 # unthrottled, 1M ticks

Measures per-tick latency from Tick.ingest_ns to post-detector timestamp.
Reports p50/p95/p99/p999/max in microseconds.
"""
import argparse
import asyncio
import statistics
import time
from typing import List

import numpy as np

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from .detector import Alert, Router
from .feed import synthetic_feed
from .pipeline import run


def _percentiles(xs_ns: List[int]) -> dict:
    a = np.asarray(xs_ns, dtype=np.int64)
    return {
        "count": int(a.size),
        "mean_us": float(a.mean() / 1000),
        "p50_us": float(np.percentile(a, 50) / 1000),
        "p95_us": float(np.percentile(a, 95) / 1000),
        "p99_us": float(np.percentile(a, 99) / 1000),
        "p999_us": float(np.percentile(a, 99.9) / 1000),
        "max_us": float(a.max() / 1000),
    }


async def _main(args: argparse.Namespace) -> None:
    symbols = args.symbols.split(",")
    feed = synthetic_feed(symbols, ticks_per_second=args.tps, anomaly_rate=args.anomaly_rate)
    router = Router()
    latencies: List[int] = []
    alerts: List[Alert] = []

    # Warmup: let EWMA state settle so early outliers don't dominate p99.
    warm: List[int] = []
    await run(feed, router, max_ticks=args.warmup, latencies_ns=warm, alerts=alerts)
    latencies.clear()
    alerts.clear()

    t0 = time.perf_counter_ns()
    await run(feed, router, max_ticks=args.ticks, latencies_ns=latencies, alerts=alerts)
    elapsed_s = (time.perf_counter_ns() - t0) / 1e9

    stats = _percentiles(latencies)
    print(f"throughput:   {args.ticks / elapsed_s:,.0f} ticks/s  ({elapsed_s:.2f}s wall)")
    print(f"per-tick e2e latency (μs):")
    for k in ("mean_us", "p50_us", "p95_us", "p99_us", "p999_us", "max_us"):
        print(f"  {k:<9} {stats[k]:>8.2f}")
    print(f"alerts emitted: {len(alerts)}  "
          f"({sum(1 for a in alerts if a.kind == 'robust_z')} z, "
          f"{sum(1 for a in alerts if a.kind == 'cusum')} cusum)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="AAPL,MSFT,SPY,NVDA")
    p.add_argument("--ticks", type=int, default=200_000)
    p.add_argument("--warmup", type=int, default=5_000)
    p.add_argument("--tps", type=int, default=0, help="ticks/sec cap; 0 = unthrottled")
    p.add_argument("--anomaly-rate", type=float, default=1e-4)
    args = p.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
