"""Diagnostic: print raw Alpaca WS trade messages so we can see the exact
field names the crypto feed uses. Run for ~20 seconds during active trading.

    python -m app.alpaca_raw --feed crypto --symbols BTC/USD
    python -m app.alpaca_raw --feed crypto --count 5
"""
import argparse
import asyncio
import json
import os
import time

from .env import load_dotenv
from .feed import ALPACA_FEED_URLS


async def run(symbols, feed_name, count, trades_only, duration) -> None:
    import websockets

    url = ALPACA_FEED_URLS.get(feed_name, f"wss://stream.data.alpaca.markets/v2/{feed_name}")
    key = os.environ["ALPACA_API_KEY"]
    sec = os.environ["ALPACA_API_SECRET"]

    async with websockets.connect(url, max_queue=None) as ws:
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": sec}))
        sub = {"action": "subscribe", "trades": symbols}
        if not trades_only:
            sub["quotes"] = symbols
        await ws.send(json.dumps(sub))
        print(f"[info] listening on {feed_name} for trades={symbols}"
              + ("" if trades_only else f" + quotes"))
        print(f"[info] will run for up to {duration:.0f}s or until {count} trades printed")

        trades_seen = 0
        quotes_seen = 0
        quote_total = 0
        deadline = time.time() + duration
        last_heartbeat = time.time()
        async for raw in ws:
            msgs = json.loads(raw)
            for m in msgs:
                t = m.get("T")
                if t == "t":
                    trades_seen += 1
                    if trades_seen <= count:
                        print(f"\n--- TRADE {trades_seen} ---")
                        print(f"keys: {list(m.keys())}")
                        print(json.dumps(m, indent=2))
                    elif trades_seen == count + 1:
                        print(f"\n[...{count} trade samples shown; still counting totals]")
                elif t == "q":
                    quote_total += 1
                    if not trades_only and quotes_seen < count:
                        quotes_seen += 1
                        print(f"\n--- QUOTE {quotes_seen} ---")
                        print(f"keys: {list(m.keys())}")
                        print(json.dumps(m, indent=2))
                elif t not in ("t", "q"):
                    print(f"[other] {t}: {m}")

            now = time.time()
            if now - last_heartbeat >= 10.0:
                last_heartbeat = now
                print(f"[heartbeat] t+{int(now - (deadline - duration))}s: "
                      f"trades={trades_seen}  quotes={quote_total}")

            if trades_only and trades_seen >= count:
                break
            if (not trades_only) and trades_seen >= count and quotes_seen >= count:
                break
            if now > deadline:
                print(f"\n[done] {trades_seen} trades, {quote_total} quotes in {duration:.0f}s")
                if trades_seen == 0:
                    print("  ⚠️  NO TRADES in window - free crypto feed may be quote-only")
                break


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="BTC/USD,ETH/USD")
    p.add_argument("--feed", default="crypto", choices=("iex", "sip", "crypto"))
    p.add_argument("--count", type=int, default=3, help="messages per kind to print")
    p.add_argument("--duration", type=float, default=180.0, help="seconds to listen (default 3 min)")
    p.add_argument("--trades-only", action="store_true", help="skip quote subscription entirely")
    args = p.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(run(syms, args.feed, args.count, args.trades_only, args.duration))


if __name__ == "__main__":
    main()
