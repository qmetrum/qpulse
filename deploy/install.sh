#!/usr/bin/env bash
# Idempotent server bootstrap for Qpulse on Ubuntu 22.04 (ARM or x86).
# Run as root (or with sudo) on a fresh EC2/Lightsail box AFTER the code has
# been rsynced to /opt/qpulse.
#
#   sudo bash /opt/qpulse/deploy/install.sh
#
# Re-running is safe — it only creates what's missing.

set -euo pipefail

REPO_ROOT="/opt/qpulse"
VENV_DIR="$REPO_ROOT/.venv"
DATA_DIR="/var/lib/qpulse"
ENV_DIR="/etc/qpulse"
ENV_FILE="$ENV_DIR/qpulse.env"
SERVICE_FILE="/etc/systemd/system/qpulse.service"
CADDY_FILE="/etc/caddy/Caddyfile"

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo bash $0)" >&2
    exit 1
fi

echo "==> apt update + system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip rsync curl debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null; then
    echo "==> installing Caddy from official repo"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
fi

echo "==> qpulse user + directories"
id qpulse >/dev/null 2>&1 || useradd --system --home "$REPO_ROOT" --shell /usr/sbin/nologin qpulse
mkdir -p "$DATA_DIR" "$ENV_DIR" /var/log/caddy
chown qpulse:qpulse "$DATA_DIR"
chmod 0750 "$DATA_DIR"

if [ ! -d "$REPO_ROOT/services/hft_anomaly_service" ]; then
    echo "ERROR: $REPO_ROOT/services/hft_anomaly_service not found — rsync the repo first" >&2
    exit 1
fi

echo "==> python venv + dependencies"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$REPO_ROOT/services/hft_anomaly_service/requirements.txt"
chown -R qpulse:qpulse "$VENV_DIR"

echo "==> environment file"
if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_ROOT/deploy/qpulse.env.example" "$ENV_FILE"
    # Generate a strong API token if the placeholder is still there
    if grep -q "REPLACE_WITH_RANDOM_HEX_64_CHARS" "$ENV_FILE"; then
        TOKEN=$(openssl rand -hex 32)
        sed -i "s|REPLACE_WITH_RANDOM_HEX_64_CHARS|$TOKEN|" "$ENV_FILE"
        echo "    generated HFT_API_TOKEN: $TOKEN"
    fi
    echo ""
    echo "    >>> EDIT $ENV_FILE to set ALPACA_API_KEY and ALPACA_API_SECRET <<<"
    echo ""
fi
chown root:qpulse "$ENV_FILE"
chmod 0640 "$ENV_FILE"

echo "==> systemd unit"
cp "$REPO_ROOT/deploy/qpulse.service" "$SERVICE_FILE"

# Health-check timer. Restart=on-failure only catches a process that exits; it
# cannot see a process that is alive but no longer receiving data. The service
# reconnects on its own, so this fires only when that has stopped working.
cp "$REPO_ROOT/deploy/qpulse-healthcheck.service" /etc/systemd/system/
cp "$REPO_ROOT/deploy/qpulse-healthcheck.timer" /etc/systemd/system/
chmod +x "$REPO_ROOT/deploy/qpulse-healthcheck.sh"

systemctl daemon-reload
systemctl enable qpulse
systemctl enable --now qpulse-healthcheck.timer

echo "==> caddy config"
if [ ! -f "$CADDY_FILE.qpulse-managed" ]; then
    cp "$REPO_ROOT/deploy/Caddyfile" "$CADDY_FILE"
    touch "$CADDY_FILE.qpulse-managed"
    echo "    >>> EDIT $CADDY_FILE — replace 'qpulse.example.com' with your real domain"
    echo "        and 'YOUR_EMAIL@example.com' with your contact email <<<"
fi
systemctl enable caddy

echo ""
echo "==> install complete. Next:"
echo "    1. Edit $ENV_FILE — set ALPACA_API_KEY + ALPACA_API_SECRET"
echo "    2. Edit $CADDY_FILE — set your domain + email"
echo "    3. Point your DNS A record at this server's public IP"
echo "    4. systemctl start qpulse caddy"
echo "    5. journalctl -u qpulse -f       (watch the startup log)"
echo "    6. curl https://your-domain/health"
