"""Webhook alert sink (Phase 2.2).

Batches alerts over a 1-second window, POSTs a JSON array to HFT_WEBHOOK_URL,
retries with exponential backoff (up to 3 attempts) before dropping. No new
deps - uses stdlib urllib through a thread executor.

Payload:
  {"source": "qpulse", "feed": "crypto", "asset_class": "crypto",
   "alerts": [{symbol, ts_ns, kind, price, score, details, severity, narrative}, ...]}

`feed` and `asset_class` travel with every batch so the receiver can tell which
context an alert came from. That matters downstream: a detector's measured recall
belongs to the data it was measured on, and a consumer must not attach a
crypto-daily-bar validation figure to, say, a live equities tick alert.

Set HFT_WEBHOOK_KEY to send a shared secret as the X-Qpulse-Key header.
"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import List, Optional

from ..bus import Bus
from ..detector import Alert

# Which asset class a feed name implies, for provenance labelling downstream.
_FEED_ASSET_CLASS = {"crypto": "crypto", "iex": "equities", "sip": "equities"}


def _post_sync(url: str, payload: dict, timeout_sec: float = 5.0,
               api_key: Optional[str] = None) -> int:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Qpulse-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return r.status


class WebhookSink:
    __slots__ = ("url", "bus", "min_score", "batch_sec", "api_key", "_feed_name",
                 "_queue", "_stop")

    def __init__(
        self,
        url: str,
        bus: Bus,
        min_score: float = 0.0,
        batch_sec: float = 1.0,
        api_key: Optional[str] = None,
        feed_name: "str | callable" = "unknown",
    ):
        self.url = url
        self.bus = bus
        self.min_score = min_score
        self.batch_sec = batch_sec
        self.api_key = api_key
        # Accepts a callable so a runtime feed switch (the /config/feed endpoint)
        # is reflected in the provenance we send. A value captured at startup
        # would keep labelling equities alerts "crypto" after a switch, and the
        # receiver would then attribute a crypto-only gate to them.
        self._feed_name = feed_name
        self._queue = bus.subscribe()
        self._stop = False

    @property
    def feed_name(self) -> str:
        f = self._feed_name
        return str(f() if callable(f) else f)

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
                    "feed": self.feed_name,
                    "asset_class": _FEED_ASSET_CLASS.get(self.feed_name, "unknown"),
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
                        status = await loop.run_in_executor(
                            None, _post_sync, self.url, payload, 5.0, self.api_key
                        )
                        if status < 400:
                            break
                    except urllib.error.HTTPError as e:
                        # urlopen raises on 4xx/5xx rather than returning the
                        # status, so client errors must be handled here or they
                        # get retried pointlessly. A bad key or a malformed
                        # batch will not fix itself in one second.
                        if e.code in (401, 403):
                            print(f"[webhook] auth rejected ({e.code}) — check HFT_WEBHOOK_KEY")
                            break
                        if 400 <= e.code < 500 and e.code != 429:
                            print(f"[webhook] request rejected ({e.code}), dropping batch")
                            break
                        print(f"[webhook] HTTP {e.code} (attempt {attempt+1}/3)")
                    except (urllib.error.URLError, OSError) as e:
                        print(f"[webhook] {type(e).__name__}: {e} (attempt {attempt+1}/3)")
                    await asyncio.sleep(backoff)
                    backoff *= 2

    def close(self) -> None:
        self._stop = True
        self.bus.unsubscribe(self._queue)
