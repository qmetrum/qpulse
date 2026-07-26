import asyncio
import time
from typing import AsyncIterator, List

from .detector import Alert, Router
from .feed import Tick


async def run(
    feed: AsyncIterator[Tick],
    router: Router,
    max_ticks: int,
    latencies_ns: List[int],
    alerts: List[Alert],
) -> None:
    """Drain `feed` through `router` up to max_ticks. Records per-tick
    end-to-end latency (feed.ingest_ns → post-detector) into latencies_ns.
    """
    i = 0
    async for tick in feed:
        emitted = router.on_tick(tick.symbol, tick.price, tick.ts_ns, tick.ingest_ns)
        latencies_ns.append(time.perf_counter_ns() - tick.ingest_ns)
        alerts.extend(emitted)
        i += 1
        if i >= max_ticks:
            return
