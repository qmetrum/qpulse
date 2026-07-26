#!/usr/bin/env bash
# Rsync local Qpulse code to a remote server. Run from your laptop.
#
# First time:
#   QPULSE_HOST=ubuntu@1.2.3.4 bash deploy/sync.sh --bootstrap
#       (rsyncs code, runs install.sh)
#
# Subsequent code updates:
#   QPULSE_HOST=ubuntu@1.2.3.4 bash deploy/sync.sh
#       (rsyncs code, restarts systemd unit)
#
# Required env:
#   QPULSE_HOST  — user@hostname or user@ip for ssh/rsync
# Optional env:
#   QPULSE_KEY   — path to ssh private key (defaults to system default)

set -euo pipefail

if [ -z "${QPULSE_HOST:-}" ]; then
    echo "ERROR: set QPULSE_HOST=user@host before running" >&2
    exit 1
fi

SSH_OPTS=()
RSYNC_RSH="ssh"
if [ -n "${QPULSE_KEY:-}" ]; then
    SSH_OPTS=(-i "$QPULSE_KEY")
    RSYNC_RSH="ssh -i $QPULSE_KEY"
fi

LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT="/opt/qpulse"

echo "==> rsync $LOCAL_ROOT -> $QPULSE_HOST:$REMOTE_ROOT"
rsync -az --delete \
    --rsh="$RSYNC_RSH" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='node_modules' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='qpulse_live.json' \
    --exclude='.env*' \
    --exclude='gate/data' \
    --exclude='gate/results' \
    "$LOCAL_ROOT/" "$QPULSE_HOST:$REMOTE_ROOT/"

if [ "${1:-}" = "--bootstrap" ]; then
    echo "==> running install.sh on remote"
    ssh "${SSH_OPTS[@]}" "$QPULSE_HOST" "sudo bash $REMOTE_ROOT/deploy/install.sh"
    echo ""
    echo "==> bootstrap complete. SSH in and complete the manual steps:"
    echo "    ssh ${SSH_OPTS[*]} $QPULSE_HOST"
    echo "    sudo nano /etc/qpulse/qpulse.env       # set Alpaca keys"
    echo "    sudo nano /etc/caddy/Caddyfile         # set domain + email"
    echo "    sudo systemctl start qpulse caddy"
else
    echo "==> restarting qpulse service"
    ssh "${SSH_OPTS[@]}" "$QPULSE_HOST" "sudo systemctl restart qpulse && sudo systemctl status qpulse --no-pager"
fi
