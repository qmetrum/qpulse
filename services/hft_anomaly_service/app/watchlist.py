"""Watchlist sync — take the symbol list from an upstream service.

Without this, the symbols Qpulse watches come from HFT_SYMBOLS, so adding a
client's holding means editing an env var and restarting a server. Polling an
upstream watchlist instead means a user creating an alert rule in the UI is
enough; the detector reconfigures itself within one poll interval.

Config:
  HFT_WATCHLIST_URL       upstream endpoint, e.g. https://host/qpulse/watchlist
  HFT_WATCHLIST_KEY       shared secret, sent as X-Qpulse-Key
                          (falls back to HFT_WEBHOOK_KEY — usually the same one)
  HFT_WATCHLIST_POLL_SEC  seconds between polls (default 300)

Deliberately conservative: any failure leaves the current subscription intact.
A watchlist service that is down, slow, or returning nonsense must never cause
the detector to stop watching what it is already watching.
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import List, Optional

# An empty upstream list is treated as "no change" rather than "unsubscribe
# everything": a bug or an empty database upstream should not silently blind
# the detector. Clearing the list is done deliberately via the API instead.
ALLOW_EMPTY_ENV = "HFT_WATCHLIST_ALLOW_EMPTY"


def _fetch_sync(url: str, api_key: Optional[str], timeout_sec: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("X-Qpulse-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return json.loads(r.read().decode())


class WatchlistSync:
    __slots__ = ("url", "api_key", "poll_sec", "controller", "state",
                 "allow_empty", "_stop", "last_error", "last_sync_ok")

    def __init__(self, url: str, controller, state, api_key: Optional[str] = None,
                 poll_sec: float = 300.0):
        self.url = url
        self.api_key = api_key
        self.poll_sec = poll_sec
        self.controller = controller
        self.state = state
        self.allow_empty = os.environ.get(ALLOW_EMPTY_ENV, "").lower() in ("1", "true", "yes")
        self._stop = False
        self.last_error: Optional[str] = None
        self.last_sync_ok: bool = False

    async def run(self) -> None:
        while not self._stop:
            try:
                await self._sync_once()
            except Exception as e:  # never let a sync failure kill the detector
                self.last_error = f"{type(e).__name__}: {e}"
                self.last_sync_ok = False
                print(f"[watchlist] sync failed: {self.last_error}")
            # Sleep in short slices so close() takes effect promptly.
            slept = 0.0
            while slept < self.poll_sec and not self._stop:
                await asyncio.sleep(min(1.0, self.poll_sec - slept))
                slept += 1.0

    async def _sync_once(self) -> None:
        # Resolve the loop here rather than taking it as an argument: passing one
        # in makes it easy to hand over a loop that isn't the running one, which
        # fails at runtime with a confusing cross-loop Future error.
        loop = asyncio.get_running_loop()
        feed = self.controller.feed_name
        url = f"{self.url}{'&' if '?' in self.url else '?'}feed={feed}"
        data = await loop.run_in_executor(None, _fetch_sync, url, self.api_key)

        desired = [str(s).strip().upper() for s in (data.get("symbols") or []) if str(s).strip()]
        if data.get("truncated"):
            print(f"[watchlist] upstream truncated the list at {len(desired)} symbols")

        if not desired and not self.allow_empty:
            self.last_sync_ok = True
            self.last_error = None
            print("[watchlist] upstream returned no symbols for "
                  f"feed={feed}; keeping current subscription "
                  f"(set {ALLOW_EMPTY_ENV}=true to allow clearing)")
            return

        current = set(self.controller.symbols)
        want = set(desired)
        to_add = sorted(want - current)
        to_remove = sorted(current - want)

        if to_add:
            await self.controller.add(to_add)
        if to_remove:
            await self.controller.remove(to_remove)

        if to_add or to_remove:
            self.state.symbols = self.controller.symbols
            print(f"[watchlist] feed={feed} +{to_add} -{to_remove} "
                  f"-> {len(self.controller.symbols)} symbols")

        self.last_sync_ok = True
        self.last_error = None

    def status(self) -> dict:
        return {
            "url": self.url,
            "poll_sec": self.poll_sec,
            "last_sync_ok": self.last_sync_ok,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        self._stop = True


def build_from_env(controller, state) -> Optional[WatchlistSync]:
    """Return a configured WatchlistSync, or None when the feature is off."""
    url = os.environ.get("HFT_WATCHLIST_URL")
    if not url:
        return None
    key = os.environ.get("HFT_WATCHLIST_KEY") or os.environ.get("HFT_WEBHOOK_KEY")
    poll = float(os.environ.get("HFT_WATCHLIST_POLL_SEC", "300"))
    return WatchlistSync(url, controller, state, api_key=key, poll_sec=poll)
