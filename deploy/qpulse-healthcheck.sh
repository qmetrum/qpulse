#!/usr/bin/env bash
# Restart Qpulse if it is up but not working.
#
# The service reconnects on its own (see AlpacaFeedController.stale_sec), so
# this is a backstop for faults that survive a reconnect — a wedged event loop,
# a permanently rejected subscription, an exhausted file descriptor.
#
# Installed as a systemd timer by install.sh; runs every 5 minutes.
set -uo pipefail

URL="${QPULSE_HEALTH_URL:-http://127.0.0.1:8080/health}"
# How long a "degraded" reading must persist before restarting. Generous, so a
# closed equities market (legitimately silent for hours) never triggers it —
# that case reports degraded but reconnects fine on its own.
MAX_DEGRADED_SEC="${QPULSE_MAX_DEGRADED_SEC:-3600}"
STATE_FILE="/var/lib/qpulse/.degraded_since"

body="$(curl -sf --max-time 10 "$URL" 2>/dev/null)" || {
    echo "healthcheck: no response from $URL — restarting"
    systemctl restart qpulse
    rm -f "$STATE_FILE"
    exit 0
}

status="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null)"

case "$status" in
    ok|starting)
        rm -f "$STATE_FILE"
        exit 0
        ;;
    down)
        echo "healthcheck: detector task is dead — restarting"
        systemctl restart qpulse
        rm -f "$STATE_FILE"
        exit 0
        ;;
esac

# degraded / reconnecting: only act if it has persisted.
now=$(date +%s)
since=$(cat "$STATE_FILE" 2>/dev/null || echo "$now")
[ -f "$STATE_FILE" ] || echo "$now" > "$STATE_FILE"
elapsed=$(( now - since ))

if [ "$elapsed" -ge "$MAX_DEGRADED_SEC" ]; then
    echo "healthcheck: status='$status' for ${elapsed}s (limit ${MAX_DEGRADED_SEC}s) — restarting"
    systemctl restart qpulse
    rm -f "$STATE_FILE"
else
    echo "healthcheck: status='$status' for ${elapsed}s — self-recovery still expected"
fi
