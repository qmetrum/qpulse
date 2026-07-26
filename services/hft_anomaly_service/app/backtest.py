"""Replay a CSV through the detector and summarize alerts.

    python -m app.backtest --input /tmp/aapl.csv
    python -m app.backtest --input /tmp/aapl.csv --alerts-out /tmp/alerts.csv
    python -m app.backtest --input /tmp/aapl.csv --z-thresh 3.5 --alpha 0.05
"""
import argparse
import asyncio
import csv
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

import numpy as np

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from .detector import Alert, PerSymbolDetector, Router
from .replay import csv_replay_feed


async def _run(
    path: str,
    alpha: float,
    z_thresh: float,
    cusum_h: float,
    cusum_k: float,
    session_gap_sec: float,
    warmup_ticks: int,
) -> tuple[list[Alert], int, float]:
    def factory(sym: str) -> PerSymbolDetector:
        return PerSymbolDetector(
            sym,
            alpha=alpha,
            z_thresh=z_thresh,
            cusum_h=cusum_h,
            cusum_k=cusum_k,
            session_gap_sec=session_gap_sec,
            warmup_ticks=warmup_ticks,
        )

    router = Router(factory=factory)
    alerts: list[Alert] = []
    tick_count = 0
    t0 = time.perf_counter_ns()

    async for tick in csv_replay_feed(path, speed=0.0):
        alerts.extend(router.on_tick(tick.symbol, tick.price, tick.ts_ns, tick.ingest_ns))
        tick_count += 1

    elapsed = (time.perf_counter_ns() - t0) / 1e9
    return alerts, tick_count, elapsed


def _summarize(alerts: List[Alert], tick_count: int, elapsed: float) -> None:
    print(f"\nticks processed: {tick_count:,}")
    print(f"elapsed:         {elapsed:.3f}s  ({tick_count / elapsed:,.0f} ticks/s)")
    print(f"alerts emitted:  {len(alerts)}  ({100 * len(alerts) / max(tick_count,1):.3f}% of ticks)")

    if not alerts:
        return

    by_kind = Counter(a.kind for a in alerts)
    print(f"  by kind:       {dict(by_kind)}")

    by_symbol: Dict[str, int] = defaultdict(int)
    for a in alerts:
        by_symbol[a.symbol] += 1
    print(f"  by symbol:     {dict(by_symbol)}")

    scores = np.asarray([a.score for a in alerts])
    print(f"  score abs:     mean={abs(scores).mean():.2f}  max={abs(scores).max():.2f}")

    # Clustering: max alerts in any rolling 1-second window
    ts = np.asarray([a.ts_ns for a in alerts], dtype=np.int64)
    ts.sort()
    window_ns = 1_000_000_000
    max_in_1s = 0
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > window_ns:
            j += 1
        max_in_1s = max(max_in_1s, i - j + 1)
    print(f"  max/1s window: {max_in_1s}")


def _write_alerts_csv(alerts: List[Alert], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ns", "symbol", "kind", "price", "score"])
        for a in alerts:
            w.writerow([a.ts_ns, a.symbol, a.kind, f"{a.price:.6f}", f"{a.score:.4f}"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--alerts-out", type=Path, default=None)
    p.add_argument("--alpha", type=float, default=0.02)
    p.add_argument("--z-thresh", type=float, default=4.0)
    p.add_argument("--cusum-h", type=float, default=5.0)
    p.add_argument("--cusum-k", type=float, default=0.5)
    p.add_argument("--session-gap-sec", type=float, default=600.0,
                   help="tick gap (seconds) treated as a session break")
    p.add_argument("--warmup-ticks", type=int, default=5,
                   help="ticks to skip scoring after a session break")
    args = p.parse_args()

    alerts, n, elapsed = asyncio.run(_run(
        args.input, args.alpha, args.z_thresh, args.cusum_h, args.cusum_k,
        args.session_gap_sec, args.warmup_ticks,
    ))
    _summarize(alerts, n, elapsed)
    if args.alerts_out:
        _write_alerts_csv(alerts, args.alerts_out)
        print(f"\nwrote alerts → {args.alerts_out}")


if __name__ == "__main__":
    main()
