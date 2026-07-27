"""WatchlistSync — reconciling the live subscription with an upstream list.

The rule that matters: a failing or empty upstream must never take symbols away
from a running detector.
"""
import asyncio

import pytest

from app.watchlist import WatchlistSync, build_from_env


class FakeController:
    def __init__(self, symbols, feed_name="crypto"):
        self.symbols = list(symbols)
        self.feed_name = feed_name
        self.added = []
        self.removed = []

    async def add(self, syms):
        self.added.append(list(syms))
        self.symbols = sorted(set(self.symbols) | set(syms))
        return syms

    async def remove(self, syms):
        self.removed.append(list(syms))
        self.symbols = [s for s in self.symbols if s not in set(syms)]
        return syms


class FakeState:
    symbols = []


def _sync(controller, payload=None, exc=None, **kw):
    """Build a WatchlistSync with the network call stubbed out.

    Returns (sync, calls) — `calls` records each requested URL. WatchlistSync
    uses __slots__, so the URL cannot be stashed on the instance.
    """
    s = WatchlistSync("http://upstream/watchlist", controller, FakeState(), **kw)
    calls = []

    def fake_fetch(url, key, timeout_sec=10.0):
        calls.append(url)
        if exc:
            raise exc
        return payload

    import app.watchlist as w
    w._fetch_sync = fake_fetch
    return s, calls


def _run(sync):
    asyncio.run(sync._sync_once())


@pytest.fixture(autouse=True)
def _restore():
    import app.watchlist as w
    original = w._fetch_sync
    yield
    w._fetch_sync = original


def test_adds_and_removes_to_match_upstream():
    c = FakeController(["BTC/USD", "DOGE/USD"])
    s, _ = _sync(c, {"symbols": ["BTC/USD", "ETH/USD"]})
    _run(s)
    assert c.added == [["ETH/USD"]]
    assert c.removed == [["DOGE/USD"]]
    assert set(c.symbols) == {"BTC/USD", "ETH/USD"}
    assert s.last_sync_ok is True


def test_no_churn_when_already_in_sync():
    c = FakeController(["BTC/USD"])
    _run(_sync(c, {"symbols": ["BTC/USD"]})[0])
    assert c.added == [] and c.removed == []


def test_empty_upstream_keeps_current_subscription():
    """A bug or empty DB upstream must not blind a running detector."""
    c = FakeController(["BTC/USD", "ETH/USD"])
    s, _ = _sync(c, {"symbols": []})
    _run(s)
    assert c.removed == []
    assert set(c.symbols) == {"BTC/USD", "ETH/USD"}
    assert s.last_sync_ok is True


def test_empty_upstream_can_clear_when_explicitly_allowed(monkeypatch):
    monkeypatch.setenv("HFT_WATCHLIST_ALLOW_EMPTY", "true")
    c = FakeController(["BTC/USD"])
    _run(_sync(c, {"symbols": []})[0])
    assert c.removed == [["BTC/USD"]]


def test_upstream_failure_leaves_symbols_untouched():
    c = FakeController(["BTC/USD"])
    s, _ = _sync(c, exc=OSError("connection refused"))
    with pytest.raises(OSError):
        _run(s)
    assert c.symbols == ["BTC/USD"]
    assert c.added == [] and c.removed == []


def test_run_loop_survives_a_failing_poll():
    """run() must swallow errors — one bad poll cannot kill the detector."""
    c = FakeController(["BTC/USD"])
    s, _ = _sync(c, exc=OSError("boom"), poll_sec=0.05)

    async def drive():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.2)
        s.close()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(drive())
    assert s.last_sync_ok is False
    assert "OSError" in (s.last_error or "")
    assert c.symbols == ["BTC/USD"]


def test_feed_is_passed_upstream_so_formats_match():
    c = FakeController([], feed_name="iex")
    s, calls = _sync(c, {"symbols": ["AAPL"]})
    _run(s)
    assert any("feed=iex" in u for u in calls), calls


def test_symbols_are_normalised():
    c = FakeController([])
    _run(_sync(c, {"symbols": [" btc/usd ", "", "  "]})[0])
    assert c.added == [["BTC/USD"]]


def test_disabled_without_url(monkeypatch):
    monkeypatch.delenv("HFT_WATCHLIST_URL", raising=False)
    assert build_from_env(FakeController([]), FakeState()) is None


def test_falls_back_to_webhook_key(monkeypatch):
    monkeypatch.setenv("HFT_WATCHLIST_URL", "http://x/watchlist")
    monkeypatch.delenv("HFT_WATCHLIST_KEY", raising=False)
    monkeypatch.setenv("HFT_WEBHOOK_KEY", "shared-secret")
    s = build_from_env(FakeController([]), FakeState())
    assert s is not None and s.api_key == "shared-secret"
