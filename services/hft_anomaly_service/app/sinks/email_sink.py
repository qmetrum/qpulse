"""Email alert sink — standalone notification delivery.

Qpulse can forward alerts to another system via the webhook sink, but a
deployment that is sold on its own needs to reach a human without one. This
sink does that over plain SMTP rather than a specific cloud provider's API, so
it works with SES, Postmark, Mailgun, Fastmail or a corporate relay without
tying the product to any of them.

Two throttles, because an unthrottled detector will email itself into a spam
folder within a day:
  * a digest window batches everything seen in the last N seconds into one mail
  * a per-symbol cooldown suppresses repeat mails about the same instrument

Config:
  HFT_EMAIL_TO            comma-separated recipients (unset => sink disabled)
  HFT_EMAIL_FROM          From address (default: qpulse@<smtp host>)
  HFT_SMTP_HOST           SMTP server (default localhost)
  HFT_SMTP_PORT           default 587
  HFT_SMTP_USER           optional; enables auth when set
  HFT_SMTP_PASSWORD       optional
  HFT_SMTP_TLS            "false" to disable STARTTLS (default on)
  HFT_EMAIL_MIN_SCORE     only email alerts at or above this |score| (default 6)
  HFT_EMAIL_DIGEST_SEC    digest window (default 60)
  HFT_EMAIL_COOLDOWN_SEC  per-symbol quiet period (default 900)
"""
import asyncio
import os
import smtplib
import time
from email.message import EmailMessage
from typing import Dict, List, Optional

from ..bus import Bus
from ..detector import Alert

# Feeds that do not carry real market data — must be stated in the email so a
# simulated or replayed alert is never mistaken for a live-market event.
_NON_LIVE_FEEDS = {"synthetic": "a synthetic (simulated) feed",
                   "csv": "a replayed historical file"}


def _send_sync(cfg: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.set_content(body)

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
        if cfg["use_tls"]:
            s.starttls()
        if cfg["user"]:
            s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


class EmailSink:
    __slots__ = ("bus", "cfg", "min_score", "digest_sec", "cooldown_sec",
                 "feed_name", "_queue", "_stop", "_last_sent", "sent_count",
                 "last_error")

    def __init__(self, bus: Bus, cfg: dict, min_score: float = 6.0,
                 digest_sec: float = 60.0, cooldown_sec: float = 900.0,
                 feed_name: "str | callable" = "unknown"):
        self.bus = bus
        self.cfg = cfg
        self.min_score = min_score
        self.digest_sec = digest_sec
        self.cooldown_sec = cooldown_sec
        self.feed_name = feed_name
        self._queue = bus.subscribe()
        self._stop = False
        self._last_sent: Dict[str, float] = {}
        self.sent_count = 0
        self.last_error: Optional[str] = None

    def _feed(self) -> str:
        f = self.feed_name
        return str(f() if callable(f) else f)

    def _admit(self, alert: Alert, now: float) -> bool:
        """Score threshold first, then per-symbol cooldown."""
        if abs(alert.score) < self.min_score:
            return False
        last = self._last_sent.get(alert.symbol)
        if last is not None and (now - last) < self.cooldown_sec:
            return False
        return True

    def _compose(self, batch: List[Alert]) -> "tuple[str, str]":
        feed = self._feed().lower()
        symbols = sorted({a.symbol for a in batch})
        if len(batch) == 1:
            a = batch[0]
            subject = f"[Qpulse] {a.symbol} {a.kind} ({a.score:+.1f})"
        else:
            subject = (f"[Qpulse] {len(batch)} anomalies on "
                       f"{', '.join(symbols[:3])}"
                       f"{'…' if len(symbols) > 3 else ''}")

        from ..narrative import render as _render, severity as _sev
        lines = []
        for a in batch:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(a.ts_ns / 1e9))
            lines.append(
                f"{ts} UTC  {a.symbol}  {a.kind}  score {a.score:+.2f}  "
                f"[{_sev(a)}]  price {a.price:,.4f}".rstrip()
            )
            note = _render(a)
            if note:
                lines.append(f"    {note}")

        caveats = [
            "A score is a statistical deviation from a rolling baseline. It is "
            "not a prediction, and it carries no accuracy or hit-rate claim.",
        ]
        if feed in _NON_LIVE_FEEDS:
            caveats.append(
                f"These alerts came from {_NON_LIVE_FEEDS[feed]}, not live market data."
            )
        caveats.append(
            "Informational only; not investment advice."
        )

        body = (
            f"Qpulse detected {len(batch)} anomaly(ies) on feed '{self._feed()}'.\n\n"
            + "\n".join(lines)
            + "\n\n"
            + "\n\n".join(caveats)
            + "\n"
        )
        return subject, body

    async def run(self) -> None:
        batch: List[Alert] = []
        last_flush = time.perf_counter()
        loop = asyncio.get_running_loop()

        while not self._stop:
            timeout = max(0.05, self.digest_sec - (time.perf_counter() - last_flush))
            try:
                alert = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                if self._admit(alert, time.time()):
                    batch.append(alert)
            except asyncio.TimeoutError:
                pass

            if batch and (time.perf_counter() - last_flush) >= self.digest_sec:
                to_send, batch = batch, []
                last_flush = time.perf_counter()
                now = time.time()
                for a in to_send:
                    self._last_sent[a.symbol] = now
                subject, body = self._compose(to_send)
                try:
                    await loop.run_in_executor(None, _send_sync, self.cfg, subject, body)
                    self.sent_count += 1
                    self.last_error = None
                    print(f"[email] sent digest of {len(to_send)} alert(s) to "
                          f"{len(self.cfg['recipients'])} recipient(s)")
                except Exception as e:  # never let delivery kill the detector
                    self.last_error = f"{type(e).__name__}: {e}"
                    print(f"[email] send failed: {self.last_error}")
            elif not batch:
                last_flush = time.perf_counter()

    def close(self) -> None:
        self._stop = True
        self.bus.unsubscribe(self._queue)


def build_from_env(bus: Bus, feed_name="unknown") -> Optional["EmailSink"]:
    """Return a configured EmailSink, or None when email is not set up."""
    to = [a.strip() for a in os.environ.get("HFT_EMAIL_TO", "").split(",") if a.strip()]
    if not to:
        return None
    host = os.environ.get("HFT_SMTP_HOST", "localhost")
    cfg = {
        "host": host,
        "port": int(os.environ.get("HFT_SMTP_PORT", "587")),
        "user": os.environ.get("HFT_SMTP_USER") or "",
        "password": os.environ.get("HFT_SMTP_PASSWORD") or "",
        "use_tls": os.environ.get("HFT_SMTP_TLS", "true").lower() not in ("0", "false", "no"),
        "sender": os.environ.get("HFT_EMAIL_FROM") or f"qpulse@{host}",
        "recipients": to,
    }
    return EmailSink(
        bus, cfg,
        min_score=float(os.environ.get("HFT_EMAIL_MIN_SCORE", "6.0")),
        digest_sec=float(os.environ.get("HFT_EMAIL_DIGEST_SEC", "60")),
        cooldown_sec=float(os.environ.get("HFT_EMAIL_COOLDOWN_SEC", "900")),
        feed_name=feed_name,
    )
