"""Webhook alert sink (Phase 2.2).

Batches alerts over a 1-second window, POSTs a JSON array to HFT_WEBHOOK_URL,
retries with exponential backoff (up to 3 attempts) before dropping. No new
deps - uses stdlib urllib through a thread executor.

Payload:
  {"source": "qpulse", "alerts": [{symbol, ts_ns, kind, price, score}, ...]}
"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import List

from ..bus import Bus
from ..detector import Alert


def _post_sync(url: str, payload: dict, timeout_sec: float = 5.0) -> int:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return r.status


class WebhookSink:
    __slots__ = ("url", "bus", "min_score", "batch_sec", "_queue", "_stop")

    def __init__(
        self,
        url: str,
        bus: Bus,
        min_score: float = 0.0,
        batch_sec: float = 1.0,
    ):
        self.url = url
        self.bus = bus
        self.min_score = min_score
        self.batch_sec = batch_sec
        self._queue = bus.subscribe()
        self._stop = False

    async def run(self) -> None:
        batch: List[Alert] = []
        last_send = time.perf_counter()
        loop = asyncio.get_running_loop()

        while not self._stop:
            timeout = max(0.01, self.batch_sec - (time.perf_counter() - last_send))
            try:
                alert = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                if abs(alert.score) >= self.min_score:
                    batch.append(alert)
            except asyncio.TimeoutError:
                pass

            if batch and (time.perf_counter() - last_send) >= self.batch_sec:
                to_send = batch
                batch = []
                last_send = time.perf_counter()
                from ..narrative import render as _render, severity as _sev
                payload = {
                    "source": "qpulse",
                    "alerts": [
                        {
                            "symbol": a.symbol, "ts_ns": a.ts_ns, "kind": a.kind,
                            "price": a.price, "score": a.score,
                            "details": a.details,
                            "severity": _sev(a),
                            "narrative": _render(a),
                        }
                        for a in to_send
                    ],
                }
                backoff = 1.0
                for attempt in range(3):
                    try:
                        status = await loop.run_in_executor(None, _post_sync, self.url, payload)
                        if status < 400:
                            break
                    except (urllib.error.URLError, OSError) as e:
                        print(f"[webhook] {type(e).__name__}: {e} (attempt {attempt+1}/3)")
                    await asyncio.sleep(backoff)
                    backoff *= 2

    def close(self) -> None:
        self._stop = True
        self.bus.unsubscribe(self._queue)
