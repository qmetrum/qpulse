"""The feed must never sit silently on a dead stream.

Regression tests for an observed failure: a live instance ran 18 hours with
only 1.5 hours of data. Alpaca had sent an `error` message, which the reader
logged and ignored, and the websocket stayed open (ping_interval keeps it alive
at the protocol level) so nothing ever raised. `Restart=on-failure` saw a
perfectly healthy process and did nothing.
"""
import asyncio

import pytest

from app.feed import AlpacaFeedController, FeedUnusable


class FakeWS:
    """Websocket stub. `script` is a list of message batches (JSON strings);
    `hang` makes it stop yielding without closing, like the real failure."""

    def __init__(self, script, hang=False):
        self.script = list(script)
        self.hang = hang
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True
        return False

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.script:
            return self.script.pop(0)
        if self.hang:
            await asyncio.sleep(3600)      # never returns — the zombie case
        raise StopAsyncIteration


def _controller(monkeypatch, ws, **kw):
    import app.feed as feed_mod

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")

    class FakeWSModule:
        @staticmethod
        def connect(url, **kwargs):
            return ws

    monkeypatch.setitem(__import__("sys").modules, "websockets", FakeWSModule)
    return AlpacaFeedController(["BTC/USD"], feed_name="crypto", **kw), feed_mod


async def _collect(controller, limit=1, timeout=3.0):
    """Pull up to `limit` events, tolerating reconnect churn."""
    out = []

    async def go():
        async for ev in controller.stream():
            out.append(ev)
            if len(out) >= limit:
                return

    try:
        await asyncio.wait_for(go(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return out


def test_error_message_triggers_reconnect_not_silence(monkeypatch):
    """The exact production failure: an error frame must not be swallowed."""
    ws = FakeWS(['[{"T":"error","code":406,"msg":"connection limit exceeded"}]'],
                hang=True)
    controller, _ = _controller(monkeypatch, ws, stale_sec=30)

    async def run():
        task = asyncio.create_task(_collect(controller, limit=1, timeout=1.5))
        await task
        return controller.health

    health = asyncio.run(run())
    assert health["reconnects"] >= 1, "error frame did not force a reconnect"
    assert "406" in (health["last_error"] or "")
    assert "connection limit" in (health["last_error"] or "").lower()


def test_silent_stream_times_out_and_reconnects(monkeypatch):
    """A stream that goes quiet without closing must not be waited on forever."""
    ws = FakeWS([], hang=True)
    controller, _ = _controller(monkeypatch, ws, stale_sec=0.2)

    async def run():
        await _collect(controller, limit=1, timeout=1.5)
        return controller.health

    health = asyncio.run(run())
    assert health["reconnects"] >= 1
    assert "no data" in (health["last_error"] or "").lower()


def test_server_closing_the_stream_reconnects(monkeypatch):
    ws = FakeWS([], hang=False)
    controller, _ = _controller(monkeypatch, ws, stale_sec=5)

    async def run():
        await _collect(controller, limit=1, timeout=1.0)
        return controller.health

    health = asyncio.run(run())
    assert health["reconnects"] >= 1
    assert "closed" in (health["last_error"] or "").lower()


def test_normal_ticks_still_flow(monkeypatch):
    """The timeout wrapper must not disturb the happy path."""
    tick = ('[{"T":"t","S":"BTC/USD","p":"42000.5","s":"0.1",'
            '"t":"2026-08-01T10:00:00Z"}]')
    ws = FakeWS([tick], hang=True)
    controller, _ = _controller(monkeypatch, ws, stale_sec=30)

    events = asyncio.run(_collect(controller, limit=1, timeout=2.0))
    assert len(events) == 1
    assert events[0].symbol == "BTC/USD"
    assert events[0].price == pytest.approx(42000.5)
    assert controller.health["reconnects"] == 0


def test_auth_and_subscribe_sent_on_connect(monkeypatch):
    ws = FakeWS([], hang=True)
    controller, _ = _controller(monkeypatch, ws, stale_sec=0.2)
    asyncio.run(_collect(controller, limit=1, timeout=0.8))
    assert any('"auth"' in s for s in ws.sent), ws.sent
    assert any('"subscribe"' in s for s in ws.sent), ws.sent


def test_stale_sec_configurable_by_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setenv("HFT_FEED_STALE_SEC", "45")
    c = AlpacaFeedController(["BTC/USD"], feed_name="crypto")
    assert c.stale_sec == 45.0


def test_health_starts_clean(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    h = AlpacaFeedController(["BTC/USD"], feed_name="crypto").health
    assert h["connected"] is False and h["reconnects"] == 0
    assert h["last_error"] is None


def test_feed_unusable_is_an_exception():
    """Raised through the existing backoff path, so reconnect logic is reused."""
    assert issubclass(FeedUnusable, Exception)


# --- /health status logic -------------------------------------------------

def _health_app(last_tick_ns=0, uptime_offset=0.0, feed_health=None):
    """Build the real app and drive /health through a TestClient."""
    import time as _time

    from fastapi.testclient import TestClient

    from app.api import make_app
    from app.bus import Bus
    from app.detector import PerSymbolDetector, Router
    from app.recent import RecentAlerts
    from app.state import RuntimeState
    from app.stats import RollingStats

    router = Router(factory=lambda s: PerSymbolDetector(s))
    app = make_app(router, RuntimeState(), RecentAlerts(), Bus(), RollingStats())
    app.state.last_tick_ns = last_tick_ns
    app.state.started_at = _time.time() - uptime_offset

    class _Task:
        def done(self):
            return False

    app.state.detector_task = _Task()
    if feed_health is not None:
        class _Ctl:
            health = feed_health
        app.state.feed_controller = _Ctl()
    return TestClient(app).get("/health").json()


def test_health_starting_before_first_tick():
    h = _health_app(last_tick_ns=0, uptime_offset=5)
    assert h["status"] == "starting"


def test_health_degraded_when_no_tick_ever_arrives():
    """The old logic reported 'ok' here — a service that never worked."""
    h = _health_app(last_tick_ns=0, uptime_offset=600)
    assert h["status"] == "degraded"


def test_health_reconnecting_when_socket_is_down():
    h = _health_app(last_tick_ns=0, uptime_offset=600,
                    feed_health={"connected": False, "reconnects": 3,
                                 "last_error": "boom", "stale_sec": 300.0})
    assert h["status"] == "reconnecting"
    assert h["feed"]["reconnects"] == 3


def test_health_ok_with_fresh_ticks():
    import time as _time
    h = _health_app(last_tick_ns=_time.perf_counter_ns(), uptime_offset=600)
    assert h["status"] == "ok"
