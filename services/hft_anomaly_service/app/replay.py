"""CSV tick-replay feed.

Expected CSV format (header required):
    ts_ns,symbol,price
    1704207600000000000,AAPL,185.64
    ...

`ts_ns` is nanoseconds since Unix epoch. `price` is a float.
Rows must be in ascending ts_ns order.

Speed modes:
  speed = 0 → emit as fast as possible (backtest mode)
  speed = 1 → real-time replay (inter-arrival preserved)
  speed > 1 → accelerated replay (e.g. 10× faster than wall clock)
"""
import asyncio
import csv
import time
from typing import AsyncIterator, Optional

from .feed import Tick


async def csv_replay_feed(
    path: str,
    speed: float = 0.0,
    symbol_filter: Optional[str] = None,
    loop: bool = False,
) -> AsyncIterator[Tick]:
    while True:
        first_src_ns: Optional[int] = None
        wall_start_ns: Optional[int] = None
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if symbol_filter and row["symbol"] != symbol_filter:
                    continue
                src_ns = int(row["ts_ns"])
                price = float(row["price"])
                sym = row["symbol"]

                if speed > 0:
                    if first_src_ns is None:
                        first_src_ns = src_ns
                        wall_start_ns = time.perf_counter_ns()
                    target_wall_ns = wall_start_ns + int((src_ns - first_src_ns) / speed)
                    sleep_ns = target_wall_ns - time.perf_counter_ns()
                    if sleep_ns > 1_000_000:
                        await asyncio.sleep(sleep_ns / 1e9)

                yield Tick(
                    symbol=sym,
                    price=price,
                    ts_ns=src_ns,
                    ingest_ns=time.perf_counter_ns(),
                )
        if not loop:
            return
