#!/usr/bin/env bash
# Reproduce every published Qpulse validation number from committed inputs.
#
#   bash gate/validate.sh              # verify (leaves the tree clean)
#   bash gate/validate.sh --refresh    # rewrite the committed result artifacts
#
# Exits nonzero if any headline number drifts, if a frozen input's hash changes,
# if a committed result artifact disagrees with a fresh run, or if any event
# catalog fails to load. This is what makes the claims in README.md and docs/
# falsifiable by anyone with a clone.
#
# Requires bash (not sh) and numpy — see services/hft_anomaly_service/requirements.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/services/hft_anomaly_service${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON:-python3}"

COMMITTED="results/eval_pitch_v1"
REFRESH=0
[ "${1:-}" = "--refresh" ] && REFRESH=1

if [ "$REFRESH" = "1" ]; then
    OUT="$COMMITTED"
else
    OUT="$(mktemp -d)"
    trap 'rm -rf "$OUT"' EXIT
fi
mkdir -p "$OUT" "$COMMITTED"

"$PY" -c 'import numpy' 2>/dev/null || {
    echo "ERROR: numpy is required (pip install -r services/hft_anomaly_service/requirements.txt)" >&2
    exit 1
}

echo "== 1/5  frozen input integrity =="
( cd gate/frozen && sha256sum -c SHA256SUMS ) | sed 's/^/  /'
( cd events/frozen && sha256sum -c SHA256SUMS ) | sed 's/^/  /'

echo
echo "== 2/5  every event catalog parses =="
"$PY" - <<'PYCHECK'
import glob, sys
from app.evaluate import load_events
bad = []
for path in sorted(glob.glob("events/**/*.csv", recursive=True)):
    try:
        print(f"  ok    {path}  ({len(load_events(path))} rows)")
    except Exception as exc:  # noqa: BLE001 - report all, fail once at the end
        bad.append(path)
        print(f"  FAIL  {path}: {exc}")
if bad:
    sys.exit(f"{len(bad)} catalog(s) failed to load")
PYCHECK

echo
echo "== 3/5  hot path v2 regenerates byte-for-byte from its pinned command =="
# The rule behind these parameters is fixed in gate/frozen/V2_BASELINE.md.
"$PY" -m app.backtest \
    --input gate/frozen/btc_replay_pitch_v1.csv \
    --alerts-out "$OUT/btc_alerts_v2_rerun.csv" \
    --alpha 0.02 --z-thresh 4.0 --cusum-h 5.0 --cusum-k 0.5 \
    --warmup-ticks 5 --session-gap-sec 604800 >/dev/null
if ! cmp -s "$OUT/btc_alerts_v2_rerun.csv" gate/frozen/btc_alerts_v2.csv; then
    echo "  FAIL  the pinned command no longer reproduces gate/frozen/btc_alerts_v2.csv"
    exit 1
fi
echo "  ok    pinned command reproduces gate/frozen/btc_alerts_v2.csv"
rm -f "$OUT/btc_alerts_v2_rerun.csv"

echo
echo "== 4/5  evaluations =="
# NO --kinds filter anywhere: the published 60.0% / 0.05 is the filterless
# number. --kinds cusum,spread_widen gives 40.0% / 0.02 on the same inputs.
#
# Each path is scored twice: against the frozen pitch-era catalog subset (what
# the published figure was measured on) and against the full committed catalog
# (which is larger, harder, and gives materially lower recall). Both are
# reported, always.
run_eval () {  # name alerts events tolerance
    "$PY" -m app.evaluate --alerts "$2" --events "$3" --tolerance "$4" \
        --json-out "$OUT/$1.json" >/dev/null
    echo "  ran   $1"
}
run_eval hot_pitch_v1     gate/frozen/btc_alerts_pitch_v1.csv      events/frozen/crypto_pitch_v1.csv 86400
run_eval hot_full_v1      gate/frozen/btc_alerts_pitch_v1.csv      events/crypto_canonical.csv       86400
run_eval hot_v2_baseline  gate/frozen/btc_alerts_v2.csv            events/frozen/crypto_pitch_v1.csv 86400
run_eval hot_full_v2      gate/frozen/btc_alerts_v2.csv            events/crypto_canonical.csv       86400
run_eval batch_pitch_v1   gate/frozen/core_dep_alerts_pitch_v1.csv events/frozen/macro_pitch_v1.csv  259200
run_eval batch_full_v1    gate/frozen/core_dep_alerts_pitch_v1.csv events/macro_canonical.csv        259200

echo
echo "== 5/5  headline numbers vs published claims =="
OUT="$OUT" COMMITTED="$COMMITTED" REFRESH="$REFRESH" "$PY" - <<'PYASSERT'
import json, os, sys

OUT, COMMITTED, REFRESH = os.environ["OUT"], os.environ["COMMITTED"], os.environ["REFRESH"] == "1"

# name, label, detected, positives, alerts_in_quiet, quiet_days
EXPECTED = [
    ("hot_pitch_v1",    "hot v1  / pitch catalog", 6, 10, 2, 42.0),
    ("hot_full_v1",     "hot v1  / FULL catalog",  8, 21, 4, 63.0),
    ("hot_v2_baseline", "hot v2  / pitch catalog", 4, 10, 0, 42.0),
    ("hot_full_v2",     "hot v2  / FULL catalog",  4, 21, 2, 63.0),
    ("batch_pitch_v1",  "batch   / pitch catalog", 6,  9, 0, 35.0),
    ("batch_full_v1",   "batch   / FULL catalog",  7, 20, 0, 63.0),
]

fail = False
for name, label, det, pos, quiet_alerts, quiet_days in EXPECTED:
    r = json.load(open(f"{OUT}/{name}.json"))["results"]
    bad = [(n, got, want) for n, got, want in [
        ("positives detected", r["positive_events_detected"], det),
        ("positives total",    r["positive_events_total"],    pos),
        ("alerts in quiet",    r["alerts_in_quiet"],          quiet_alerts),
        ("quiet days",         round(r["quiet_total_days"], 6), quiet_days),
    ] if got != want]
    lo, hi = r["detection_rate_ci95"]
    flo, fhi = r["fp_per_day_ci95"]
    n_out = len(r["positive_events_outside_alert_coverage"])
    note = f"  ({n_out} uncoverable)" if n_out else ""
    # Print MEASURED values only — never the expectations, or a mismatch would
    # be displayed as if it were the result.
    print(f"  {'FAIL' if bad else 'ok':<4s}  {label:<24s}  recall {r['detection_rate']:5.1%} "
          f"({r['positive_events_detected']}/{r['positive_events_total']}) "
          f"[{lo:.0%}-{hi:.0%}]   FP {r['fp_per_day_in_quiet']:.2f}/day "
          f"[{flo:.2f}-{fhi:.2f}]{note}")
    for n, got, want in bad:
        print(f"        {n}: got {got}, expected {want}")
        fail = True

    # The committed artifact must match a fresh run, ignoring only the timestamp.
    committed_path = f"{COMMITTED}/{name}.json"
    if REFRESH:
        continue
    if not os.path.exists(committed_path):
        print(f"        committed artifact missing: {committed_path} (run --refresh)")
        fail = True
        continue
    fresh = json.load(open(f"{OUT}/{name}.json"))
    old = json.load(open(committed_path))
    for d in (fresh, old):
        d.pop("generated_at", None)
    if fresh != old:
        print(f"        committed artifact {committed_path} disagrees with a fresh run (run --refresh)")
        fail = True

if fail:
    sys.exit("\npublished numbers did NOT reproduce")
print("\nAll published numbers reproduced from committed inputs.")
print("Note: the FULL-catalog rows are the harder, more honest measure; the pitch-catalog")
print("rows exist because they are what the originally published figures were computed on.")
PYASSERT
