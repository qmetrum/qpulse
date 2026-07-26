"""Pull recent alerts from a running live service and summarize them.

    python -m app.inspect_alerts
    python -m app.inspect_alerts --url http://localhost:8080 --limit 500
"""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.request import urlopen


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args()

    alerts = json.loads(urlopen(f"{args.url}/alerts/recent?limit={args.limit}").read())
    try:
        stats = json.loads(urlopen(f"{args.url}/config").read())
    except Exception:
        stats = {}

    if not alerts:
        print("no alerts in memory ring")
        return

    print(f"=== {len(alerts)} recent alerts (ring capacity 500; older ones evicted) ===\n")

    ts = sorted(a["ts_ns"] for a in alerts)
    span_sec = (ts[-1] - ts[0]) / 1e9 if len(ts) > 1 else 0
    span_min = span_sec / 60

    by_kind = Counter(a["kind"] for a in alerts)
    by_sym = Counter(a["symbol"] for a in alerts)
    by_pair = Counter((a["symbol"], a["kind"]) for a in alerts)

    print(f"window span:      {span_min:.1f} min ({span_sec:.0f}s)")
    print(f"overall rate:     {len(alerts)/max(span_min,1e-6):.1f} alerts/min")
    print(f"active symbols:   {stats.get('symbols', sorted(by_sym))}")
    print(f"trade thresholds: z={stats.get('default_z_thresh')} "
          f"h={stats.get('default_cusum_h')} k={stats.get('default_cusum_k')} "
          f"alpha={stats.get('alpha')}")
    print(f"micro thresholds: spread_z={stats.get('spread_z_thresh')} "
          f"obi_fire={stats.get('obi_extreme_thresh')} "
          f"obi_rearm={stats.get('obi_rearm_thresh')} "
          f"qrate_z={stats.get('qrate_z_thresh')} "
          f"qrate_cooldown={stats.get('qrate_cooldown_sec')}s")
    print()

    print("by kind:")
    for k, c in by_kind.most_common():
        pct = 100 * c / len(alerts)
        rate = c / max(span_min, 1e-6)
        print(f"  {k:<20s}  {c:>4d}  ({pct:>4.1f}%)  {rate:>5.1f}/min")
    print()

    print("by symbol:")
    for s, c in by_sym.most_common():
        rate = c / max(span_min, 1e-6)
        print(f"  {s:<15s}  {c:>4d}  {rate:>5.1f}/min")
    print()

    print("symbol × kind:")
    for (s, k), c in by_pair.most_common():
        print(f"  {s:<15s}  {k:<20s}  {c}")
    print()

    # Top 10 by |score|
    top = sorted(alerts, key=lambda a: abs(a["score"]), reverse=True)[:10]
    print("top 10 by |score|:")
    for a in top:
        dt = datetime.fromtimestamp(a["ts_ns"] / 1e9, tz=timezone.utc)
        print(f"  {dt.isoformat(timespec='milliseconds')}  "
              f"{a['symbol']:<10s}  {a['kind']:<20s}  "
              f"price={a['price']:>12.4f}  score={a['score']:+7.2f}")
    print()

    # Clustering - bursts in fixed windows
    max_1s = max_10s = max_60s = 0
    for win_ns, slot in ((1_000_000_000, 1), (10_000_000_000, 2), (60_000_000_000, 3)):
        j = 0
        m = 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > win_ns:
                j += 1
            m = max(m, i - j + 1)
        if slot == 1: max_1s = m
        elif slot == 2: max_10s = m
        else: max_60s = m
    print(f"max alerts in rolling 1s:  {max_1s}")
    print(f"max alerts in rolling 10s: {max_10s}")
    print(f"max alerts in rolling 60s: {max_60s}")

    # Score distribution per kind
    print("\nscore |abs| by kind (p50 / p95 / max):")
    scores_by_kind = defaultdict(list)
    for a in alerts:
        scores_by_kind[a["kind"]].append(abs(a["score"]))
    for k, xs in scores_by_kind.items():
        print(f"  {k:<20s}  {_pct(xs,50):>6.2f}  {_pct(xs,95):>6.2f}  {max(xs):>6.2f}")


if __name__ == "__main__":
    main()
