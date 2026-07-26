import asyncio
from typing import Any, Set


class Bus:
    """Async pub/sub. Per-subscriber bounded queue; slow consumers drop, never
    backpressure the publisher (detector must not be blocked by a UI client).
    """

    def __init__(self, maxsize: int = 1000):
        self._subs: Set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, msg: Any) -> None:
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow consumer - drop
