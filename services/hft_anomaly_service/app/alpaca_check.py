"""Alpaca WebSocket connectivity test.

    python -m app.alpaca_check                                   # 60s, default symbols, IEX
    python -m app.alpaca_check --duration 30 --max-trades 100
    python -m app.alpaca_check --feed sip                        # paid SIP feed

Requires ALPACA_API_KEY and ALPACA_API_SECRET in the environment.
Bypasses the auto-reconnect loop in feed.py so failures are surfaced immediately.

What it checks:
  1. TCP/TLS to stream.data.alpaca.markets
  2. Server hello message
  3. Auth success (bad key/secret surfaces here, not silently)
  4. Subscribe success for requested symbols
  5. Live trade delivery (or silence - IEX is US-hours only)
  6. Timestamp sanity: alpaca ts_ns vs local clock skew

Expected outputs by scenario:
  - market open, valid keys:   auth ok → sub ok → N trades received → summary
  - market closed, valid keys: auth ok → sub ok → 0 trades (explained)
  - bad keys:                  auth FAILED with Alpaca error code
  - network block:             ConnectError or TimeoutError at step 1
"""
import argparse
import asyncio
import json
import os
import time
from collections import Counter
from typing import List

from .env import load_dotenv
from .feed import ALPACA_FEED_URLS, _parse_rfc3339_ns


def _pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, int(len(xs) * p / 100))
    return xs[k]


async def _run(symbols: List[str], duration_s: float, max_trades: int, feed_name: str) -> int:
    import websockets

    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        print("FAIL: ALPACA_API_KEY and ALPACA_API_SECRET must be set")
        return 2

    url = ALPACA_FEED_URLS.get(feed_name, f"wss://stream.data.alpaca.markets/v2/{feed_name}")
    print(f"[1/5] connecting: {url}")

    try:
        ws_ctx = websockets.connect(url, max_queue=None, open_timeout=10)
    except Exception as e:
        print(f"FAIL connect: {type(e).__name__}: {e}")
        return 3

    async with ws_ctx as ws:
        print("      ok - TCP/TLS established")

        # Step 2: server hello
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            hello = json.loads(raw)
            print(f"[2/5] server hello: {hello}")
        except asyncio.TimeoutError:
            print("FAIL: no server hello within 5s")
            return 4

        # Step 3: auth
        await ws.send(json.dumps({"action": "auth", "key": api_key, "secret": api_secret}))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msgs = json.loads(raw)
        auth_ok = any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in msgs)
        if not auth_ok:
            print(f"FAIL auth: {msgs}")
            print("  hint: regenerate paper keys at alpaca.markets → Paper Account → API Keys")
            return 5
        print(f"[3/5] auth ok")

        # Step 4: subscribe
        await ws.send(json.dumps({"action": "subscribe", "trades": symbols}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        sub = json.loads(raw)
        sub_msg = next((m for m in sub if m.get("T") == "subscription"), None)
        if not sub_msg:
            print(f"FAIL subscribe: {sub}")
            return 6
        print(f"[4/5] subscribed: trades={sub_msg.get('trades', [])}")

        # Step 5: receive trades
        print(f"[5/5] listening for up to {duration_s:.0f}s or {max_trades} trades…")
        print("     (IEX market hours: 09:30–16:00 ET weekdays; silence outside that is normal)")
        print()

        deadline = time.perf_counter() + duration_s
        count = 0
        by_sym: Counter = Counter()
        skew_ms: List[float] = []
        prices: dict = {}
        first_ingest_ns = None

        try:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0 or count >= max_trades:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    events = json.loads(raw)
                except ValueError:
                    continue
                now_local_ns = time.time_ns()
                recv_ns = time.perf_counter_ns()
                for m in events:
                    if m.get("T") != "t":
                        continue
                    sym = m["S"]
                    price = float(m["p"])
                    ts_ns = _parse_rfc3339_ns(m["t"])
                    skew = (now_local_ns - ts_ns) / 1e6
                    skew_ms.append(skew)
                    count += 1
                    by_sym[sym] += 1
                    if sym not in prices:
                        prices[sym] = [price, price]
                        if first_ingest_ns is None:
                            first_ingest_ns = recv_ns
                    prices[sym][1] = price
                    if count <= 10:
                        print(f"  trade #{count}: {sym:5s}  ${price:>9.4f}  "
                              f"ts={ts_ns}  skew={skew:+.0f}ms")
                    elif count == 11:
                        print("  … (further trades collapsed; will report totals)")
                    elif count % 100 == 0:
                        print(f"  … {count} trades")
        finally:
            pass

    # Summary
    print()
    print("=== summary ===")
    if count == 0:
        print("  0 trades received.")
        print("  If US market is open (09:30–16:00 ET Mon–Fri), something's wrong.")
        print("  If it's closed, this is expected - the auth+subscribe above is the green signal.")
        return 0

    elapsed_s = max(1e-6, (time.perf_counter_ns() - first_ingest_ns) / 1e9)
    print(f"  trades received:   {count}")
    print(f"  duration:          {elapsed_s:.1f}s")
    print(f"  rate:              {count / elapsed_s:.1f} trades/s")
    print(f"  symbols:           {dict(by_sym)}")
    print(f"  clock skew (ms):   p50={_pct(skew_ms, 50):+.0f}  p95={_pct(skew_ms, 95):+.0f}  p99={_pct(skew_ms, 99):+.0f}")
    print(f"  price range:")
    for sym, (first, last) in prices.items():
        delta = (last - first) / first * 100 if first else 0
        print(f"    {sym:5s}  {first:>9.4f} → {last:>9.4f}  ({delta:+.2f}%)")
    return 0


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=None,
                   help="comma-separated; defaults to AAPL,MSFT,SPY,NVDA "
                        "(or BTC/USD,ETH/USD if --feed crypto)")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--max-trades", type=int, default=500)
    p.add_argument("--feed", default=os.environ.get("ALPACA_FEED", "iex"),
                   choices=("iex", "sip", "crypto"))
    args = p.parse_args()

    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.feed == "crypto":
        syms = ["BTC/USD", "ETH/USD"]
    else:
        syms = ["AAPL", "MSFT", "SPY", "NVDA"]
    rc = asyncio.run(_run(syms, args.duration, args.max_trades, args.feed))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
