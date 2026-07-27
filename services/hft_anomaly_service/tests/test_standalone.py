"""Qpulse must remain sellable and runnable on its own.

Two things are asserted here:
  1. No Qsight integration is ever required — every hook is opt-in by env var.
  2. A standalone deployment can still reach a human, via the email sink.

Test 1 is a guardrail: the Qsight-facing features are convenient and it would be
easy to let one quietly become load-bearing.
"""
import asyncio

import pytest

from app.bus import Bus
from app.detector import Alert
from app.sinks.email_sink import EmailSink, build_from_env as build_email_sink
from app.watchlist import build_from_env as build_watchlist_sync

# Every environment variable that points Qpulse at another service.
INTEGRATION_ENV = [
    "HFT_WEBHOOK_URL", "HFT_WEBHOOK_KEY",
    "HFT_WATCHLIST_URL", "HFT_WATCHLIST_KEY",
]


@pytest.fixture
def bare_env(monkeypatch):
    """A machine that has never heard of Qsight."""
    for name in INTEGRATION_ENV + ["HFT_EMAIL_TO"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _alert(symbol="BTC/USD", kind="robust_z", score=7.5):
    return Alert(ts_ns=1_700_000_000_000_000_000, ingest_ns=1_700_000_000_000_000_000,
                 symbol=symbol, kind=kind, price=42000.0, score=score, details={})


# --- 1. independence ------------------------------------------------------

def test_no_qsight_import_anywhere():
    """A code-level dependency would make the standalone product unshippable."""
    import pathlib
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for py in app_dir.rglob("*.py"):
        text = py.read_text(errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if "qsight" in stripped.lower() or "forecasting_service" in stripped.lower():
                offenders.append(f"{py.name}: {stripped}")
    assert offenders == [], offenders


def test_integrations_are_all_opt_in(bare_env):
    """With no integration env vars, nothing Qsight-facing is constructed."""
    assert build_watchlist_sync(object(), object()) is None
    assert build_email_sink(Bus()) is None


def test_core_pipeline_runs_with_no_integration_env(bare_env):
    """Detector -> bus still works standalone; alerts are produced and consumed."""
    from app.detector import Router, PerSymbolDetector

    router = Router(factory=lambda s: PerSymbolDetector(s, warmup_ticks=1, min_obs=0))
    produced = []
    price = 100.0
    for i in range(300):
        price *= 1.0 + (0.05 if i == 250 else 0.0001)
        produced.extend(router.on_tick("AAPL", price, i * 1_000_000_000, i * 1_000_000_000))
    assert produced, "standalone detector produced no alerts on an injected jump"


# --- 2. standalone delivery ----------------------------------------------

def _sink(monkeypatch, **over):
    sent = []
    import app.sinks.email_sink as es
    monkeypatch.setattr(es, "_send_sync",
                        lambda cfg, subject, body: sent.append((subject, body)))
    cfg = {"host": "localhost", "port": 587, "user": "", "password": "",
           "use_tls": False, "sender": "qpulse@local", "recipients": ["ops@example.com"]}
    kw = {"min_score": 6.0, "digest_sec": 0.05, "cooldown_sec": 900.0,
          "feed_name": "crypto"}
    kw.update(over)
    return EmailSink(Bus(), cfg, **kw), sent


def _drive(sink, alerts, seconds=0.4):
    async def go():
        task = asyncio.create_task(sink.run())
        for a in alerts:
            sink.bus.publish(a)
        await asyncio.sleep(seconds)
        sink.close()
        task.cancel()
    asyncio.run(go())


def test_email_sent_for_a_significant_alert(monkeypatch):
    sink, sent = _sink(monkeypatch)
    _drive(sink, [_alert(score=9.0)])
    assert sent, "no email produced"
    subject, body = sent[0]
    assert "BTC/USD" in subject and "robust_z" in body
    assert "not investment advice" in body


def test_low_score_alerts_are_not_emailed(monkeypatch):
    sink, sent = _sink(monkeypatch, min_score=6.0)
    _drive(sink, [_alert(score=2.0)])
    assert sent == []


def test_repeat_alerts_on_one_symbol_are_cooled_down(monkeypatch):
    """Alert fatigue is the failure mode; one symbol must not spam."""
    sink, sent = _sink(monkeypatch, cooldown_sec=3600)
    _drive(sink, [_alert(score=9.0)], seconds=0.25)
    _drive(sink, [_alert(score=9.5)], seconds=0.25)
    assert len(sent) == 1, f"cooldown did not suppress the repeat: {sent}"


def test_multiple_symbols_are_digested_into_one_email(monkeypatch):
    sink, sent = _sink(monkeypatch)
    _drive(sink, [_alert(symbol="BTC/USD", score=9.0),
                  _alert(symbol="ETH/USD", score=8.0)])
    assert len(sent) == 1, "expected a single digest"
    subject, body = sent[0]
    assert "2 anomalies" in subject
    assert "BTC/USD" in body and "ETH/USD" in body


def test_synthetic_feed_is_disclosed(monkeypatch):
    sink, sent = _sink(monkeypatch, feed_name="synthetic")
    _drive(sink, [_alert(score=9.0)])
    assert "not live market data" in sent[0][1]


def test_email_never_claims_accuracy(monkeypatch):
    sink, sent = _sink(monkeypatch)
    _drive(sink, [_alert(score=9.0)])
    body = sent[0][1].lower()
    assert "no accuracy or hit-rate claim" in body
    for banned in ("% recall", "accuracy of", "hit rate of", "60%"):
        assert banned not in body


def test_smtp_failure_does_not_kill_the_sink(monkeypatch):
    import app.sinks.email_sink as es

    def boom(cfg, subject, body):
        raise OSError("smtp unreachable")

    monkeypatch.setattr(es, "_send_sync", boom)
    cfg = {"host": "localhost", "port": 587, "user": "", "password": "",
           "use_tls": False, "sender": "q@local", "recipients": ["o@example.com"]}
    sink = EmailSink(Bus(), cfg, min_score=1.0, digest_sec=0.05)
    _drive(sink, [_alert(score=9.0)])
    assert sink.last_error and "OSError" in sink.last_error
    assert sink.sent_count == 0


def test_build_from_env_wires_recipients(monkeypatch):
    monkeypatch.setenv("HFT_EMAIL_TO", "a@example.com, b@example.com")
    monkeypatch.setenv("HFT_SMTP_HOST", "smtp.example.com")
    sink = build_email_sink(Bus())
    assert sink is not None
    assert sink.cfg["recipients"] == ["a@example.com", "b@example.com"]
    assert sink.cfg["sender"] == "qpulse@smtp.example.com"
