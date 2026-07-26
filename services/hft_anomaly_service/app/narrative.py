"""Sentence-form narrative renderer for Alert objects.

Each kind has a template that turns the structured `details` dict into a
human-readable line. Example outputs:

  ANOMALY: Price deviation +3.21σ from baseline on AAPL (threshold ±4.0)
  WARNING: Spread 8.2bps on BTC/USD, +5.8σ from baseline 1.4bps
  ALERT  : Volume spike on ETH/USD, trade size 12.4 (2.7× rolling mean)
  ANOMALY: Crossed book on AAPL - bid 175.42 > ask 175.40 (data error or extreme stress)

Severity (ANOMALY / WARNING / ALERT / INFO) is derived from |score| and
kind-specific overrides - `crossed_book` is always ANOMALY regardless of
score magnitude, for example.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .detector import Alert


def severity(alert: "Alert") -> str:
    """Map alert to ANOMALY / WARNING / ALERT / INFO."""
    # Kind-specific overrides
    if alert.kind == "crossed_book":
        return "ANOMALY"
    if alert.kind == "burst":
        return "WARNING"
    if alert.kind == "obi_extreme":
        return "ALERT"
    s = abs(alert.score)
    if s >= 8.0:
        return "ANOMALY"
    if s >= 5.0:
        return "WARNING"
    if s >= 3.0:
        return "ALERT"
    return "INFO"


def _fmt(v, prec: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 1e6 or (v != 0 and abs(v) < 1e-3):
            return f"{v:.{prec}g}"
        return f"{v:.{prec}f}"
    return str(v)


def render(alert: "Alert") -> str:
    """One-line sentence for the tape, Slack message, email subject, etc."""
    sev = severity(alert)
    sym = alert.symbol
    d = alert.details or {}
    score = alert.score

    if alert.kind == "robust_z":
        thr = d.get("z_threshold", "?")
        return f"{sev}: Price deviation {score:+.2f}σ from baseline on {sym} (threshold ±{_fmt(thr, 1)})"

    if alert.kind == "cusum":
        h = d.get("cusum_h", "?")
        return f"{sev}: Cumulative drift {score:+.2f} on {sym} (CUSUM threshold ±{_fmt(h, 1)})"

    if alert.kind == "spread_widen":
        bps = d.get("spread_bps", 0)
        baseline = d.get("baseline_bps", 0)
        return f"{sev}: Spread {_fmt(bps, 1)}bps on {sym}, {score:+.2f}σ from baseline {_fmt(baseline, 1)}bps"

    if alert.kind == "obi_extreme":
        side = d.get("side", "?")
        return f"{sev}: Order book {abs(score)*100:.0f}% {side}-dominant on {sym}"

    if alert.kind == "quote_rate_spike":
        rate = d.get("rate_qps", "?")
        baseline = d.get("baseline_qps", "?")
        return f"{sev}: Quote rate {_fmt(rate, 1)}/s on {sym}, {score:+.2f}σ from baseline {_fmt(baseline, 1)}/s"

    if alert.kind == "regime_shift":
        ks = d.get("ks_statistic", score)
        thr = d.get("ks_threshold", "?")
        return f"{sev}: Return distribution shifted on {sym} (KS={_fmt(ks, 3)}, threshold {_fmt(thr, 2)})"

    if alert.kind == "burst":
        ratio = d.get("intensity_ratio", score)
        n = d.get("alerts_in_window", "?")
        win = d.get("window_sec", "?")
        return f"{sev}: Alert clustering on {sym} - {n} alerts in {_fmt(win, 0)}s ({_fmt(ratio, 1)}× baseline)"

    if alert.kind == "dependence_shift":
        return f"{sev}: Cross-asset dependence shifted {score:+.2f}σ across the basket"

    if alert.kind == "volume_spike":
        size = d.get("size", "?")
        ratio = d.get("ratio_to_baseline", "?")
        return f"{sev}: Volume spike on {sym}, trade size {_fmt(size, 4)} ({_fmt(ratio, 1)}× rolling mean)"

    if alert.kind == "microprice_drift":
        side = d.get("side", "?")
        return f"{sev}: Microprice drift {score:+.2f}σ toward {side} on {sym} (informational asymmetry)"

    if alert.kind == "crossed_book":
        bid = d.get("bid", 0)
        ask = d.get("ask", 0)
        return f"{sev}: Crossed book on {sym} - bid {_fmt(bid, 4)} > ask {_fmt(ask, 4)} (data error or extreme stress)"

    return f"{sev}: {alert.kind} on {sym} (score {score:+.2f})"
