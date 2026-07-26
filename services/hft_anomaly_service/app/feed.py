import asyncio
import calendar
import datetime as _dt
import json
import math
import os
import random
import time
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, List, Optional, Union


@dataclass(slots=True)
class Tick:
    symbol: str
    price: float
    ts_ns: int          # exchange/source timestamp (ns since epoch)
    ingest_ns: int      # local monotonic ns at feed entry (for latency accounting)
    size: float = 0.0   # trade size in shares / units; 0 if unavailable (synthetic, replay)


@dataclass(slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts_ns: int
    ingest_ns: int


async def synthetic_feed(
    symbols: Iterable[str],
    ticks_per_second: int = 10_000,
    anomaly_rate: float = 1e-4,
    seed: int = 42,
) -> AsyncIterator[Tick]:
    """GBM-ish synthetic ticks with occasional jumps.

    Timing is approximate; for latency benchmarking we push as fast as possible
    and only yield to the loop every ~1000 ticks. Pass ticks_per_second=0 for
    unthrottled generation.
    """
    rng = random.Random(seed)
    prices = {s: 100.0 for s in symbols}
    syms = list(symbols)
    period_ns = int(1e9 / ticks_per_second) if ticks_per_second else 0
    next_ns = time.perf_counter_ns()
    i = 0
    while True:
        sym = syms[i % len(syms)]
        p = prices[sym]
        dt = 1e-4
        mu, sigma = 0.0, 0.5
        p *= math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * rng.gauss(0, 1))
        if rng.random() < anomaly_rate:
            p *= 1.0 + (0.02 if rng.random() < 0.5 else -0.02)
        prices[sym] = p

        now = time.perf_counter_ns()
        yield Tick(symbol=sym, price=p, ts_ns=time.time_ns(), ingest_ns=now)

        i += 1
        if period_ns:
            next_ns += period_ns
            sleep_ns = next_ns - time.perf_counter_ns()
            if sleep_ns > 1_000_000:
                await asyncio.sleep(sleep_ns / 1e9)
            elif i % 1024 == 0:
                await asyncio.sleep(0)
        elif i % 1024 == 0:
            await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Alpaca WebSocket adapter - free IEX feed or paid SIP feed.
# --------------------------------------------------------------------------

def _parse_rfc3339_ns(s: str) -> int:
    """Parse 'YYYY-MM-DDTHH:MM:SS[.fffffffff]Z' → ns since epoch.

    Alpaca sends trade timestamps with up to 9 fractional digits (nanoseconds).
    datetime.timestamp() goes through float and loses precision, so we do the
    epoch conversion with calendar.timegm and track ns fraction separately.
    """
    if s.endswith("Z"):
        s = s[:-1]
    date_part, _, frac = s.partition(".")
    tm = _dt.datetime.strptime(date_part, "%Y-%m-%dT%H:%M:%S")
    sec = calendar.timegm(tm.timetuple())
    if frac:
        frac = (frac + "000000000")[:9]
        return sec * 1_000_000_000 + int(frac)
    return sec * 1_000_000_000


ALPACA_FEED_URLS = {
    "iex":    "wss://stream.data.alpaca.markets/v2/iex",         # free, US equities, market hours only
    "sip":    "wss://stream.data.alpaca.markets/v2/sip",         # paid, full US equities
    "crypto": "wss://stream.data.alpaca.markets/v1beta3/crypto/us",  # free, 24/7, symbols like BTC/USD
}


async def alpaca_trades_feed(
    symbols: Iterable[str],
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    feed_name: str = "iex",
) -> AsyncIterator[Tick]:
    """Subscribe to Alpaca trades WebSocket and yield Tick instances.

    feed_name: "iex" (free US equities), "sip" (paid full-market), or
    "crypto" (free 24/7; symbols must use slash format, e.g. BTC/USD).

    Requires ALPACA_API_KEY / ALPACA_API_SECRET env vars (or explicit args).
    Automatically reconnects with exponential backoff on connection loss.
    """
    import websockets

    api_key = api_key or os.environ.get("ALPACA_API_KEY")
    api_secret = api_secret or os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit("ALPACA_API_KEY and ALPACA_API_SECRET must be set")

    syms: List[str] = list(symbols)
    url = ALPACA_FEED_URLS.get(feed_name, f"wss://stream.data.alpaca.markets/v2/{feed_name}")
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(url, max_queue=None, ping_interval=10) as ws:
                await ws.send(json.dumps({"action": "auth", "key": api_key, "secret": api_secret}))
                await ws.send(json.dumps({"action": "subscribe", "trades": syms}))
                backoff = 1.0

                async for raw in ws:
                    recv_ns = time.perf_counter_ns()
                    try:
                        msgs = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msgs, list):
                        continue
                    for m in msgs:
                        t = m.get("T")
                        if t == "t":
                            yield Tick(
                                symbol=m["S"],
                                price=float(m["p"]),
                                ts_ns=_parse_rfc3339_ns(m["t"]),
                                ingest_ns=recv_ns,
                                size=float(m.get("s", 0) or 0),
                            )
                        elif t == "error":
                            print(f"[alpaca] error: {m}")
                        # "success", "subscription" messages are informational; ignore
        except Exception as e:
            print(f"[alpaca] {type(e).__name__}: {e}; reconnect in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def alpaca_market_feed(
    symbols: Iterable[str],
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    feed_name: str = "iex",
    trades: bool = True,
    quotes: bool = True,
) -> AsyncIterator[Union[Tick, Quote]]:
    """Subscribe to both trades and quotes on Alpaca and yield Tick/Quote events.

    Same auth/reconnect behaviour as alpaca_trades_feed but emits both event
    types so microstructure features can be computed alongside trade anomalies.
    """
    import websockets

    api_key = api_key or os.environ.get("ALPACA_API_KEY")
    api_secret = api_secret or os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit("ALPACA_API_KEY and ALPACA_API_SECRET must be set")

    syms: List[str] = list(symbols)
    url = ALPACA_FEED_URLS.get(feed_name, f"wss://stream.data.alpaca.markets/v2/{feed_name}")
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(url, max_queue=None, ping_interval=10) as ws:
                await ws.send(json.dumps({"action": "auth", "key": api_key, "secret": api_secret}))
                sub: dict = {"action": "subscribe"}
                if trades:
                    sub["trades"] = syms
                if quotes:
                    sub["quotes"] = syms
                await ws.send(json.dumps(sub))
                backoff = 1.0

                async for raw in ws:
                    recv_ns = time.perf_counter_ns()
                    try:
                        msgs = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msgs, list):
                        continue
                    for m in msgs:
                        t = m.get("T")
                        if t == "t":
                            yield Tick(
                                symbol=m["S"],
                                price=float(m["p"]),
                                ts_ns=_parse_rfc3339_ns(m["t"]),
                                ingest_ns=recv_ns,
                                size=float(m.get("s", 0) or 0),
                            )
                        elif t == "q":
                            yield Quote(
                                symbol=m["S"],
                                bid=float(m["bp"]),
                                ask=float(m["ap"]),
                                bid_size=float(m["bs"]),
                                ask_size=float(m["as"]),
                                ts_ns=_parse_rfc3339_ns(m["t"]),
                                ingest_ns=recv_ns,
                            )
                        elif t == "error":
                            print(f"[alpaca] error: {m}")
        except Exception as e:
            print(f"[alpaca] {type(e).__name__}: {e}; reconnect in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# --------------------------------------------------------------------------
# Alpaca FeedController - mutable subscription state for runtime add/remove
# --------------------------------------------------------------------------

class AlpacaFeedController:
    """Wrap an Alpaca WebSocket connection with mutable subscription state.

    The detector loop iterates over `stream()`. Concurrently, callers (e.g. the
    `POST /config/symbols` endpoint) call `add()` / `remove()` to subscribe or
    unsubscribe symbols on the live WS without reconnecting. Alpaca's v2/iex,
    v2/sip, and v1beta3/crypto endpoints all support runtime subscribe /
    unsubscribe actions on an existing authenticated connection.
    """

    def __init__(
        self,
        initial_symbols: Iterable[str],
        feed_name: str = "iex",
        trades: bool = True,
        quotes: bool = True,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.feed_name = feed_name
        self.trades = trades
        self.quotes = quotes
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET")
        if not self.api_key or not self.api_secret:
            raise SystemExit("ALPACA_API_KEY and ALPACA_API_SECRET must be set")
        self._symbols: "set[str]" = set(initial_symbols)
        self._ws = None
        self._lock = asyncio.Lock()
        self._rebuild = asyncio.Event()  # set by switch_feed() to force reconnect

    @property
    def symbols(self) -> "list[str]":
        return sorted(self._symbols)

    @property
    def is_crypto(self) -> bool:
        return self.feed_name == "crypto"

    async def stream(self) -> AsyncIterator:
        """Consume this for the detector loop. Yields Tick / Quote events.
        Reconnects with exponential backoff on connection loss. URL and
        symbol set are read fresh on every reconnect, so switch_feed()
        seamlessly swaps feeds via the rebuild signal.
        """
        import websockets

        backoff = 1.0
        while True:
            url = ALPACA_FEED_URLS.get(
                self.feed_name,
                f"wss://stream.data.alpaca.markets/v2/{self.feed_name}",
            )
            try:
                async with websockets.connect(url, max_queue=None, ping_interval=10) as ws:
                    async with self._lock:
                        self._ws = ws
                    await ws.send(json.dumps({
                        "action": "auth", "key": self.api_key, "secret": self.api_secret,
                    }))
                    syms = sorted(self._symbols)
                    if syms:
                        sub: dict = {"action": "subscribe"}
                        if self.trades:
                            sub["trades"] = syms
                        if self.quotes:
                            sub["quotes"] = syms
                        await ws.send(json.dumps(sub))
                    backoff = 1.0

                    async for raw in ws:
                        recv_ns = time.perf_counter_ns()
                        try:
                            msgs = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(msgs, list):
                            continue
                        for m in msgs:
                            t = m.get("T")
                            if t == "t":
                                yield Tick(
                                    symbol=m["S"],
                                    price=float(m["p"]),
                                    ts_ns=_parse_rfc3339_ns(m["t"]),
                                    ingest_ns=recv_ns,
                                    size=float(m.get("s", 0) or 0),
                                )
                            elif t == "q":
                                yield Quote(
                                    symbol=m["S"],
                                    bid=float(m["bp"]),
                                    ask=float(m["ap"]),
                                    bid_size=float(m["bs"]),
                                    ask_size=float(m["as"]),
                                    ts_ns=_parse_rfc3339_ns(m["t"]),
                                    ingest_ns=recv_ns,
                                )
                            elif t == "error":
                                print(f"[alpaca] error: {m}")
            except Exception as e:
                async with self._lock:
                    self._ws = None
                print(f"[alpaca] {type(e).__name__}: {e}; reconnect in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def add(self, syms: "list[str]") -> "list[str]":
        """Subscribe to new symbols. Returns the list actually added (deduped)."""
        new = sorted(set(syms) - self._symbols)
        if not new:
            return []
        async with self._lock:
            self._symbols |= set(new)
            if self._ws is not None:
                msg: dict = {"action": "subscribe"}
                if self.trades:
                    msg["trades"] = new
                if self.quotes:
                    msg["quotes"] = new
                await self._ws.send(json.dumps(msg))
        return new

    async def remove(self, syms: "list[str]") -> "list[str]":
        """Unsubscribe symbols. Returns the list actually removed."""
        gone = sorted(set(syms) & self._symbols)
        if not gone:
            return []
        async with self._lock:
            self._symbols -= set(gone)
            if self._ws is not None:
                msg: dict = {"action": "unsubscribe"}
                if self.trades:
                    msg["trades"] = gone
                if self.quotes:
                    msg["quotes"] = gone
                await self._ws.send(json.dumps(msg))
        return gone

    async def switch_feed(self, new_feed: str, new_symbols: Optional["list[str]"] = None) -> None:
        """Switch to a different Alpaca feed (iex / sip / crypto).

        Symbols from the old feed are dropped (they are usually format-
        incompatible across feeds - e.g. crypto uses 'BTC/USD' slash format,
        equities use plain tickers). Pass `new_symbols` to subscribe right
        after the reconnect; otherwise the new feed starts empty.

        Implementation: closes the live WS to break the inner async-for
        loop. The outer reconnect loop reads `feed_name` and `_symbols`
        fresh on the next iteration, so the new feed comes up with the
        new URL and new subscription.
        """
        if new_feed not in ALPACA_FEED_URLS and new_feed != self.feed_name:
            raise ValueError(f"unknown feed: {new_feed}; want one of {list(ALPACA_FEED_URLS)}")
        async with self._lock:
            self.feed_name = new_feed
            self._symbols = set(new_symbols or [])
            ws = self._ws
            self._ws = None
        # Close OUTSIDE the lock so the stream loop's lock-free access is fine
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Polygon WebSocket adapter (stub - wire up when entitled)
# --------------------------------------------------------------------------
#
# async def polygon_trades_feed(symbols, api_key=None) -> AsyncIterator[Tick]:
#     import websockets
#     api_key = api_key or os.environ["POLYGON_API_KEY"]
#     url = "wss://socket.polygon.io/stocks"
#     subs = ",".join(f"T.{s}" for s in symbols)
#     async with websockets.connect(url, max_queue=None) as ws:
#         await ws.send(json.dumps({"action": "auth", "params": api_key}))
#         await ws.recv()
#         await ws.send(json.dumps({"action": "subscribe", "params": subs}))
#         async for raw in ws:
#             recv_ns = time.perf_counter_ns()
#             for ev in json.loads(raw):
#                 if ev.get("ev") != "T":
#                     continue
#                 yield Tick(
#                     symbol=ev["sym"],
#                     price=float(ev["p"]),
#                     ts_ns=int(ev["t"]) * 1_000_000,
#                     ingest_ns=recv_ns,
#                 )
