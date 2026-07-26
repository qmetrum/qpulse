"""Headless recorder: capture a feed to CSV for later backtesting/sweeping.

    python -m app.record --source alpaca --symbols AAPL,MSFT --out /data/ticks.csv --duration 3600
    python -m app.record --source synthetic --out /tmp/synth.csv --duration 10
"""
import argparse
import asyncio
import os
import time

from .env import load_dotenv
from .feed import alpaca_trades_feed, synthetic_feed
from .recorder import CsvTickRecorder


async def _run(feed, recorder: CsvTickRecorder, duration_s: float) -> None:
    deadline = time.perf_counter() + duration_s if duration_s > 0 else None
    last_print = 0
    try:
        async for tick in feed:
            recorder.write(tick)
            if recorder.count - last_print >= 1000:
                last_print = recorder.count
                print(f"  recorded {recorder.count:,} ticks")
            if deadline and time.perf_counter() >= deadline:
                break
    finally:
        recorder.close()
    print(f"wrote {recorder.count:,} ticks")


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=("synthetic", "alpaca"), default="alpaca")
    p.add_argument("--symbols", default=os.environ.get("HFT_SYMBOLS", "AAPL,MSFT,SPY,NVDA"))
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl-C")
    p.add_argument("--tps", type=int, default=100, help="synthetic only")
    args = p.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.source == "alpaca":
        feed = alpaca_trades_feed(syms, feed_name=os.environ.get("ALPACA_FEED", "iex"))
    else:
        feed = synthetic_feed(syms, ticks_per_second=args.tps)

    recorder = CsvTickRecorder(args.out)
    print(f"recording {args.source} → {args.out}  symbols={syms}"
          + (f"  (stops at {args.duration:.0f}s)" if args.duration > 0 else "  (Ctrl-C to stop)"))
    try:
        asyncio.run(_run(feed, recorder, args.duration))
    except KeyboardInterrupt:
        recorder.close()
        print(f"\ninterrupted; wrote {recorder.count:,} ticks")


if __name__ == "__main__":
    main()
