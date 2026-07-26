"""Write an incoming Tick stream to CSV for later replay via app.backtest / app.sweep."""
import csv
from pathlib import Path
from typing import AsyncIterator

from .feed import Tick


class CsvTickRecorder:
    __slots__ = ("_f", "_w", "_flush_every", "_count")

    def __init__(self, path: str, flush_every: int = 100):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(["ts_ns", "symbol", "price"])
        self._flush_every = flush_every
        self._count = 0

    def write(self, tick: Tick) -> None:
        self._w.writerow([tick.ts_ns, tick.symbol, f"{tick.price:.6f}"])
        self._count += 1
        if self._count % self._flush_every == 0:
            self._f.flush()

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        try:
            self._f.flush()
        finally:
            self._f.close()


async def record_tap(feed, recorder: CsvTickRecorder) -> AsyncIterator:
    """Pass-through: writes trade events (Ticks) to the recorder, forwards
    every event (Tick or Quote) to the consumer. Quotes are skipped by the
    CSV recorder because the replay format is trades-only."""
    try:
        async for event in feed:
            if isinstance(event, Tick):
                recorder.write(event)
            yield event
    finally:
        recorder.close()
