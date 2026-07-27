#!/usr/bin/env bash
# Run the whole Qpulse -> Qsight loop locally and watch alerts arrive.
#
#   bash scripts/local-demo.sh          (override QSIGHT_REPO if the monorepo lives elsewhere)
#
# Starts the Qsight backend on :8000, creates anomaly rules, runs Qpulse on the
# synthetic feed (which injects anomalies deliberately, so alerts appear in
# seconds), then tails what landed. Ctrl-C stops everything.
set -uo pipefail

QSIGHT="${QSIGHT_REPO:-$HOME/Documents/qmetrum_project}/services/forecasting_service_py"
QPULSE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/services/hft_anomaly_service"
KEY="local-demo-key"
SYMBOLS="AAPL,MSFT,SPY,NVDA"

cleanup() {
    echo
    echo "==> stopping"
    [ -n "${QPULSE_PID:-}" ] && kill "$QPULSE_PID" 2>/dev/null
    [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "==> starting Qsight backend on :8000 (first boot loads TensorFlow, ~15s)"
cd "$QSIGHT"
# services/forecasting_service_py/.env sets COGNITO_* and load_dotenv() picks it
# up, which would make the backend demand a real Cognito token. Blanking these
# two (load_dotenv does not override values already in the environment) puts it
# back on the local-dev path where X-User-Id is accepted.
COGNITO_REGION= COGNITO_USER_POOL_ID= \
QPULSE_INGEST_KEY="$KEY" ALERT_SCHEDULER_ENABLED=0 \
    python3 -m uvicorn app.main:app --port 8000 --log-level warning &
API_PID=$!

for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break
    sleep 2
done
curl -sf http://127.0.0.1:8000/healthz >/dev/null || { echo "backend failed to start"; exit 1; }
echo "    backend up"

echo "==> clearing any rules left by a previous run"
# Alerts fan out to EVERY matching rule, so re-running without this would show
# each alert once per duplicate rule.
curl -s "http://127.0.0.1:8000/alerts" -H "X-User-Id: 1" 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    items = []
for a in items:
    if (a.get("extra_config") or {}).get("detector") == "qpulse":
        print(a["id"])
' | while read -r id; do
    curl -s -o /dev/null -X DELETE "http://127.0.0.1:8000/alerts/$id" -H "X-User-Id: 1"
    echo "    removed old rule $id"
done

echo "==> creating anomaly rules for $SYMBOLS"
IFS=',' read -ra TICKERS <<< "$SYMBOLS"
for t in "${TICKERS[@]}"; do
    curl -s -o /dev/null -X POST http://127.0.0.1:8000/alerts \
        -H "Content-Type: application/json" -H "X-User-Id: 1" \
        -d "{\"name\":\"$t anomaly (Qpulse)\",\"ticker\":\"$t\",\"alert_type\":\"anomaly\",
             \"is_active\":true,\"extra_config\":{\"detector\":\"qpulse\",\"cooldown_seconds\":5}}"
    echo "    rule: $t"
done

echo "==> starting Qpulse on the synthetic feed -> http://127.0.0.1:8000/qpulse/ingest"
cd "$QPULSE"
HFT_WEBHOOK_URL="http://127.0.0.1:8000/qpulse/ingest" \
HFT_WEBHOOK_KEY="$KEY" \
HFT_WEBHOOK_MIN_SCORE=0 \
HFT_DB_PATH="/tmp/qpulse_demo.db" \
HFT_CONFIG_PATH="/tmp/qpulse_demo.json" \
    python3 -m app.live --source synthetic --symbols "$SYMBOLS" \
        --tps 200 --anomaly-rate 0.02 --port 8080 &
QPULSE_PID=$!

sleep 12
echo
echo "    Qpulse UI:      http://localhost:8080/"
echo "    Qsight API:     http://127.0.0.1:8000"
echo "    (for the dashboard: cd services/frontend_nextjs && npm run dev -> localhost:3000)"
echo
echo "==> alerts landing in Qsight (Ctrl-C to stop):"
while true; do
    curl -s "http://127.0.0.1:8000/alerts/events?limit=5&triggered_only=true&detector_source=qpulse" \
        -H "X-User-Id: 1" 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    sys.exit(0)
if not items:
    print("  (waiting for alerts...)")
for i in items:
    p = i.get("payload") or {}
    q = p.get("qpulse", {})
    gate = p.get("reference_gate")
    tag = "scored offline on " + gate if gate else "experimental"
    when = str(i.get("evaluated_at", ""))[11:19]
    print("  {}  {:<6s} {:<14s} score={:7.2f}  {:<8s} [{}] feed={}".format(
        when, i.get("ticker", "?"), q.get("kind", "?"), q.get("score", 0.0),
        str(q.get("severity") or ""), tag, q.get("feed")))
'
    echo "  ---"
    sleep 10
done
