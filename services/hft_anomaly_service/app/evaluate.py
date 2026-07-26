"""Labeled-event detection evaluator (Phase 0.5 of the Qpulse plan).

    python -m app.evaluate --alerts /tmp/alerts.csv --events events/sample.csv
    python -m app.evaluate --alerts /tmp/alerts.csv --events events/sample.csv \
                            --tolerance 60 --kinds cusum,spread_widen

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
from collections import Counter, defaultdict
from dataclasses import dataclass
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


def load_events(path: str) -> List[Event]:
    out: List[Event] = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
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

    return {
        "alerts_total": len(alerts_f),
        # Positive metrics
        "positive_events_total": len(positive_events),
        "positive_events_detected": len(detected_pos),
        "detection_rate": detection_rate,
        "mean_ttd_ms": (sum(ttd_ns) / len(ttd_ns) / 1e6) if ttd_ns else 0.0,
        "median_ttd_ms": (sorted(ttd_ns)[len(ttd_ns) // 2] / 1e6) if ttd_ns else 0.0,
        "missed_events": missed_pos,
        # Quiet-window metrics (clean FP signal)
        "quiet_windows_total": len(quiet_events),
        "quiet_windows_violated": len(quiet_violated),
        "quiet_violation_rate": quiet_violation_rate,
        "alerts_in_quiet": len(alerts_in_quiet),
        "quiet_total_days": quiet_total_sec / 86400.0,
        "fp_per_day_in_quiet": fp_per_day,
        # Breakdowns
        "per_event_type": per_type,
        "per_kind_attributed_positive": dict(kind_attributed),
        "per_kind_in_quiet": dict(kind_in_quiet),
        "per_kind_total": dict(kind_total),
        # Unattributed = alerts not in any labeled window
        "unattributed": len(unattributed),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--alerts", required=True, help="CSV from backtest --alerts-out")
    p.add_argument("--events", required=True, help="labeled events CSV (ts_ns,symbol,event_type,description)")
    p.add_argument("--tolerance", type=float, default=60.0,
                   help="±seconds from event ts considered a detection")
    p.add_argument("--kinds", default=None,
                   help="comma-separated list of alert kinds to consider (default: all)")
    args = p.parse_args()

    alerts = load_alerts(args.alerts)
    events = load_events(args.events)
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None

    res = evaluate(alerts, events, args.tolerance, kinds)

    print(f"\nAlerts: {res['alerts_total']}  |  ±{args.tolerance:.0f}s tolerance for point events")
    print()
    print("== Detection (positive events) ==")
    print(f"  Detection rate:        {res['detection_rate']:.1%}  "
          f"({res['positive_events_detected']}/{res['positive_events_total']})")
    if res['positive_events_detected'] > 0:
        print(f"  Median time-to-detect: {res['median_ttd_ms']:+.0f} ms")
    print()
    print("== Precision (negative controls / quiet windows) ==")
    print(f"  Quiet windows:         {res['quiet_windows_total']}  "
          f"({res['quiet_total_days']:.0f} total days)")
    print(f"  Windows violated:      {res['quiet_windows_violated']}/{res['quiet_windows_total']}  "
          f"({res['quiet_violation_rate']:.0%})")
    print(f"  Alerts in quiet:       {res['alerts_in_quiet']}  "
          f"({res['fp_per_day_in_quiet']:.2f}/day)")
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
