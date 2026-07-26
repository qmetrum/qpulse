"""Labeled-event detection evaluator (Phase 0.5 of the Qpulse plan).

    python -m app.evaluate --alerts /tmp/alerts.csv --events events/sample.csv
    python -m app.evaluate --alerts /tmp/alerts.csv --events events/sample.csv \
                            --tolerance 60 --json-out results/eval.json

The published hot-path figure (60.0% recall / 0.05 FP-per-day) is computed
with NO --kinds filter. Passing --kinds cusum,spread_widen yields 40.0% / 0.02
on the same inputs; earlier revisions of this docstring implied otherwise.
See gate/validate.sh for the pinned invocations.

Ground-truth events are supplied as a CSV with columns:
    ts_ns,symbol,event_type,description

An event is considered DETECTED if at least one alert fires on the same
symbol within ±tolerance seconds of the event's ts_ns. Produces:
  - detection rate (recall) overall and per event_type
  - mean time-to-detect (TTD)
  - false-positive rate: alerts NOT within tolerance of ANY event
  - breakdown by alert kind

Notes on event sources (no API fetch; we curate/download externally):
  - SEC halt notices:    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=25-NSE
  - FINRA short-sale CB: https://www.finra.org/filing-reporting/short-sale-circuit-breaker
  - Earnings calendars:  e.g. Investing.com, Nasdaq, Seeking Alpha
  - Crypto: exchange halt announcements, regulatory actions
"""
import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass(slots=True)
class Event:
    ts_ns: int
    symbol: str
    event_type: str
    description: str
    polarity: str = "positive"   # "positive" | "quiet"
    duration_sec: float = 0.0    # 0 means point event (use ±tolerance)


@dataclass(slots=True)
class AlertRow:
    ts_ns: int
    symbol: str
    kind: str
    price: float
    score: float


def wilson_ci(successes: int, total: int, z: float = 1.96) -> "tuple[float, float]":
    """Wilson score interval for a binomial proportion. Behaves sanely at the
    0/n and n/n extremes, where the normal approximation collapses to a point."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def poisson_rate_ci(count: int, exposure: float, conf: float = 0.95) -> "tuple[float, float]":
    """Garwood exact CI for a Poisson rate (events per unit exposure).

    With count=0 the lower bound is 0 and the upper bound is the rule-of-three
    style bound 3.689/exposure at conf=0.95."""
    if exposure <= 0:
        return (0.0, 0.0)
    tail = (1.0 - conf) / 2.0
    lo = 0.0 if count == 0 else _chi2_quantile(tail, 2 * count) / 2.0
    hi = _chi2_quantile(1.0 - tail, 2 * (count + 1)) / 2.0
    return (lo / exposure, hi / exposure)


def _chi2_quantile(p: float, df: int) -> float:
    """Chi-square quantile by bisection on the regularised lower incomplete
    gamma function — avoids a scipy dependency in the evaluator hot path."""
    if df <= 0:
        return 0.0
    lo, hi = 0.0, float(df) + 10.0 * math.sqrt(2.0 * df) + 20.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _gammainc_lower_reg(df / 2.0, mid / 2.0) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _gammainc_lower_reg(a: float, x: float) -> float:
    """Regularised lower incomplete gamma P(a, x), series + continued fraction."""
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        term = 1.0 / a
        total = term
        n = a
        for _ in range(500):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    # Continued fraction for Q(a, x), then P = 1 - Q.
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return 1.0 - q


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_events(path: str) -> List[Event]:
    out: List[Event] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        expected = list(reader.fieldnames or [])
        for lineno, row in enumerate(reader, start=2):
            # Both directions matter. Extra fields land in row[None] and shift
            # every column; missing ones come back as None values, which would
            # give polarity=None and drop the row from BOTH the positive and
            # quiet denominators without any error — silently changing a
            # published recall. Neither is allowed to pass.
            if None in row:
                raise ValueError(
                    f"{path}:{lineno}: {len(expected) + len(row[None])} fields "
                    f"against a {len(expected)}-column header — catalog is malformed"
                )
            missing = [k for k, v in row.items() if v is None]
            if missing:
                raise ValueError(
                    f"{path}:{lineno}: missing value(s) for {', '.join(missing)} "
                    f"against a {len(expected)}-column header — catalog is malformed"
                )
            out.append(Event(
                ts_ns=int(row["ts_ns"]),
                symbol=row["symbol"],
                event_type=row.get("event_type", "unknown"),
                description=row.get("description", ""),
                polarity=row.get("polarity", "positive"),
                duration_sec=float(row.get("duration_sec", 0) or 0),
            ))
    return out


def load_alerts(path: str) -> List[AlertRow]:
    out: List[AlertRow] = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            out.append(AlertRow(
                ts_ns=int(row["ts_ns"]),
                symbol=row["symbol"],
                kind=row["kind"],
                price=float(row["price"]),
                score=float(row["score"]),
            ))
    return out


def _event_window(ev: Event, tolerance_ns: int) -> "tuple[int, int]":
    """Return (start_ns, end_ns) of the matching window for an event.
    Point events (duration=0) use ±tolerance; windowed events use
    [ts_ns, ts_ns + duration_sec] with no extra tolerance."""
    if ev.duration_sec > 0:
        return ev.ts_ns, ev.ts_ns + int(ev.duration_sec * 1e9)
    return ev.ts_ns - tolerance_ns, ev.ts_ns + tolerance_ns


def evaluate(
    alerts: List[AlertRow],
    events: List[Event],
    tolerance_sec: float,
    kinds: Optional[List[str]] = None,
) -> dict:
    tolerance_ns = int(tolerance_sec * 1e9)

    if kinds:
        kinds_set = set(kinds)
        alerts_f = [a for a in alerts if a.kind in kinds_set]
    else:
        alerts_f = list(alerts)

    alerts_by_symbol: Dict[str, List[AlertRow]] = defaultdict(list)
    for a in alerts_f:
        alerts_by_symbol[a.symbol].append(a)
    for syms in alerts_by_symbol.values():
        syms.sort(key=lambda a: a.ts_ns)

    positive_events = [ev for ev in events if ev.polarity == "positive"]
    quiet_events = [ev for ev in events if ev.polarity == "quiet"]

    # --- Positive detection + TTD ---
    detected_pos: List[Event] = []
    missed_pos: List[Event] = []
    ttd_ns: List[int] = []
    attributed_to_pos: set = set()  # (ts, sym, kind) tuples
    for ev in positive_events:
        start, end = _event_window(ev, tolerance_ns)
        window_alerts = [
            a for a in alerts_by_symbol.get(ev.symbol, [])
            if start <= a.ts_ns <= end
        ]
        if window_alerts:
            detected_pos.append(ev)
            post = [a for a in window_alerts if a.ts_ns >= ev.ts_ns]
            ref = post[0] if post else min(window_alerts, key=lambda a: abs(a.ts_ns - ev.ts_ns))
            ttd_ns.append(ref.ts_ns - ev.ts_ns)
            for a in window_alerts:
                attributed_to_pos.add((a.ts_ns, a.symbol, a.kind))
        else:
            missed_pos.append(ev)

    # --- Quiet-window spurious fires (clean FP signal) ---
    quiet_total_sec = sum(ev.duration_sec for ev in quiet_events)
    alerts_in_quiet: List[AlertRow] = []
    quiet_violated: List[Event] = []
    for ev in quiet_events:
        start, end = _event_window(ev, tolerance_ns)
        fires = [a for a in alerts_by_symbol.get(ev.symbol, [])
                 if start <= a.ts_ns <= end]
        if fires:
            quiet_violated.append(ev)
            alerts_in_quiet.extend(fires)

    # --- Unattributed alerts (possibly TPs on un-catalogued events) ---
    quiet_keys = {(a.ts_ns, a.symbol, a.kind) for a in alerts_in_quiet}
    unattributed = [
        a for a in alerts_f
        if (a.ts_ns, a.symbol, a.kind) not in attributed_to_pos
        and (a.ts_ns, a.symbol, a.kind) not in quiet_keys
    ]

    detection_rate = len(detected_pos) / len(positive_events) if positive_events else 0.0
    quiet_violation_rate = (
        len(quiet_violated) / len(quiet_events) if quiet_events else 0.0
    )
    # True FPR per day from quiet windows only (the clean measure)
    fp_per_day = (len(alerts_in_quiet) / (quiet_total_sec / 86400.0)) if quiet_total_sec else 0.0

    by_type_total = Counter(ev.event_type for ev in positive_events)
    by_type_detected = Counter(ev.event_type for ev in detected_pos)
    per_type = {
        t: (by_type_detected.get(t, 0), by_type_total[t],
            by_type_detected.get(t, 0) / by_type_total[t] if by_type_total[t] else 0.0)
        for t in by_type_total
    }

    kind_attributed: Counter = Counter()
    for (_, _, k) in attributed_to_pos:
        kind_attributed[k] += 1
    kind_in_quiet = Counter(a.kind for a in alerts_in_quiet)
    kind_total = Counter(a.kind for a in alerts_f)

    quiet_total_days = quiet_total_sec / 86400.0
    recall_lo, recall_hi = wilson_ci(len(detected_pos), len(positive_events))
    fp_lo, fp_hi = poisson_rate_ci(len(alerts_in_quiet), quiet_total_days)

    # Alerts can only detect events inside the span they actually cover; events
    # outside it are structural misses, not detector failures. Measured on the
    # UNFILTERED alerts and widened by the tolerance, so that a --kinds filter
    # cannot reclassify a genuine detector miss as "impossible to detect".
    all_ts = [a.ts_ns for a in alerts]
    coverage = (min(all_ts) - tolerance_ns, max(all_ts) + tolerance_ns) if all_ts else None
    outside_coverage = [
        ev.event_type for ev in positive_events
        if coverage is None or not (coverage[0] <= ev.ts_ns <= coverage[1])
    ]

    # Detection-time resolution: with daily bars every timestamp is a multiple
    # of 86400 s, so a "0 ms" TTD means "same bar", not sub-millisecond. Report
    # the input's granularity so the figure cannot be read as latency.
    all_stamps = sorted({a.ts_ns for a in alerts} | {ev.ts_ns for ev in events})
    gaps = [b - a for a, b in zip(all_stamps, all_stamps[1:]) if b > a]
    ts_resolution_ns = math.gcd(*gaps) if len(gaps) > 1 else (gaps[0] if gaps else 0)

    return {
        "alerts_total": len(alerts_f),
        "alerts_coverage_ts_ns": list(coverage) if coverage else None,
        "timestamp_resolution_ns": ts_resolution_ns,
        "ttd_per_event_ns": ttd_ns,
        # Positive metrics
        "positive_events_total": len(positive_events),
        "positive_events_detected": len(detected_pos),
        "detection_rate": detection_rate,
        "detection_rate_ci95": [recall_lo, recall_hi],
        "positive_events_outside_alert_coverage": outside_coverage,
        "mean_ttd_ms": (sum(ttd_ns) / len(ttd_ns) / 1e6) if ttd_ns else 0.0,
        "median_ttd_ms": (sorted(ttd_ns)[len(ttd_ns) // 2] / 1e6) if ttd_ns else 0.0,
        "missed_events": missed_pos,
        # Quiet-window metrics (clean FP signal)
        "quiet_windows_total": len(quiet_events),
        "quiet_windows_violated": len(quiet_violated),
        "quiet_violation_rate": quiet_violation_rate,
        "alerts_in_quiet": len(alerts_in_quiet),
        "quiet_total_days": quiet_total_days,
        "fp_per_day_in_quiet": fp_per_day,
        "fp_per_day_ci95": [fp_lo, fp_hi],
        # Breakdowns
        "per_event_type": per_type,
        "per_kind_attributed_positive": dict(kind_attributed),
        "per_kind_in_quiet": dict(kind_in_quiet),
        "per_kind_total": dict(kind_total),
        # Unattributed = alerts not in any labeled window
        "unattributed": len(unattributed),
    }


def _print_ttd(res: dict) -> None:
    """Report time-to-detect at the resolution the input actually has.

    Quoting "0 ms" off daily bars invites the reader to hear sub-millisecond
    detection latency, when all it can mean is "the same bar". Anything coarser
    than a second is reported in its own units, with the early fires that a
    median hides shown alongside."""
    res_ns = res.get("timestamp_resolution_ns", 0)
    ttds = res.get("ttd_per_event_ns", [])
    if res_ns >= 1_000_000_000:
        unit, div = ("day", 86_400e9) if res_ns >= 86_400_000_000_000 else ("s", 1e9)
        median = sorted(ttds)[len(ttds) // 2] / div if ttds else 0.0
        print(f"  Median time-to-detect: {median:+.0f} {unit}  "
              f"(input resolution is {res_ns / div:.0f} {unit} — this is a bar count, not latency)")
        early = sum(1 for t in ttds if t < 0)
        if early:
            print(f"    of {len(ttds)} detections, {early} fired BEFORE the labeled "
                  f"timestamp (earliest {min(ttds) / div:+.0f} {unit})")
    else:
        print(f"  Median time-to-detect: {res['median_ttd_ms']:+.0f} ms")


def write_json_artifact(args, res: dict) -> None:
    """Persist the run so a published number can be traced to its inputs.

    Everything needed to re-run and to detect input drift goes in: the paths,
    their content hashes, the tolerance, and the kinds filter."""
    payload = {
        "schema": "qpulse.evaluate/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "alerts_path": args.alerts,
            "alerts_sha256": sha256_file(args.alerts),
            "events_path": args.events,
            "events_sha256": sha256_file(args.events),
            "tolerance_sec": args.tolerance,
            "kinds_filter": args.kinds,
        },
        "results": {k: v for k, v in res.items() if k != "missed_events"},
        "missed_events": [
            {"ts_ns": ev.ts_ns, "symbol": ev.symbol, "event_type": ev.event_type,
             "description": ev.description}
            for ev in res["missed_events"]
        ],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
    with open(args.json_out, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"\nwrote {args.json_out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--alerts", required=True, help="CSV from backtest --alerts-out")
    p.add_argument("--events", required=True, help="labeled events CSV (ts_ns,symbol,event_type,description)")
    p.add_argument("--tolerance", type=float, default=60.0,
                   help="±seconds from event ts considered a detection")
    p.add_argument("--kinds", default=None,
                   help="comma-separated list of alert kinds to consider (default: all)")
    p.add_argument("--json-out", default=None,
                   help="also write a JSON artifact (inputs + sha256 + metrics) to this path")
    args = p.parse_args()

    alerts = load_alerts(args.alerts)
    events = load_events(args.events)
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None

    res = evaluate(alerts, events, args.tolerance, kinds)

    if args.json_out:
        write_json_artifact(args, res)

    print(f"\nAlerts: {res['alerts_total']}  |  ±{args.tolerance:.0f}s tolerance for point events")
    print()
    print("== Detection (positive events) ==")
    lo, hi = res['detection_rate_ci95']
    print(f"  Detection rate:        {res['detection_rate']:.1%}  "
          f"({res['positive_events_detected']}/{res['positive_events_total']})  "
          f"[95% CI {lo:.1%}-{hi:.1%}]")
    if res['positive_events_detected'] > 0:
        _print_ttd(res)
    if res['positive_events_outside_alert_coverage']:
        names = ", ".join(res['positive_events_outside_alert_coverage'])
        print(f"  NOTE: {len(res['positive_events_outside_alert_coverage'])} event(s) lie outside "
              f"the alerts file's time coverage and cannot be detected: {names}")
    print()
    print("== Precision (negative controls / quiet windows) ==")
    print(f"  Quiet windows:         {res['quiet_windows_total']}  "
          f"({res['quiet_total_days']:.0f} total days)")
    print(f"  Windows violated:      {res['quiet_windows_violated']}/{res['quiet_windows_total']}  "
          f"({res['quiet_violation_rate']:.0%})")
    fp_lo, fp_hi = res['fp_per_day_ci95']
    print(f"  Alerts in quiet:       {res['alerts_in_quiet']}  "
          f"({res['fp_per_day_in_quiet']:.2f}/day)  [95% CI {fp_lo:.2f}-{fp_hi:.2f}]")
    print()
    print("== Breakdown ==")
    print(f"  Unattributed alerts:   {res['unattributed']}  "
          f"(outside labeled windows - may be real unlabeled events)")
    print()
    print("  By event_type (detected/total):")
    for t, (d, n, rate) in sorted(res['per_event_type'].items(), key=lambda kv: -kv[1][1]):
        print(f"    {t:<28s}  {d}/{n}  ({rate:.0%})")
    print()
    print("  Alert kinds - attributed positive / in quiet / total:")
    for k in sorted(res['per_kind_total'], key=lambda k: -res['per_kind_total'][k]):
        total = res['per_kind_total'][k]
        attr = res['per_kind_attributed_positive'].get(k, 0)
        quiet_ = res['per_kind_in_quiet'].get(k, 0)
        print(f"    {k:<20s}  pos={attr:<4d}  quiet={quiet_:<4d}  total={total}")

    if res['missed_events']:
        print(f"\nMissed positive events ({len(res['missed_events'])}):")
        for ev in res['missed_events'][:10]:
            print(f"  {ev.ts_ns}  {ev.symbol:<10s}  {ev.event_type:<24s}  {ev.description}")


if __name__ == "__main__":
    main()
