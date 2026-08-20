#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$ROOT_DIR/.env"
BINARY="$ROOT_DIR/build/evolution-go"
SERVICE_NAME="evolution-go"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER=$(id -un)
CURRENT_GROUP=$(id -gn)

log() {
  printf '[evolution-go-install] %s\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

require_command sudo
require_command systemctl

if [ ! -f "$ENV_FILE" ]; then
  echo "No .env found at $ENV_FILE. Copy .env.example to .env and configure it first." >&2
  exit 1
fi

if [ ! -x "$BINARY" ]; then
  log "Binary not found at $BINARY, building it now."
  (cd "$ROOT_DIR" && make build)
fi

log "Installing systemd unit for ${SERVICE_NAME} (WorkingDirectory=${ROOT_DIR}, ExecStart=${BINARY})."

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Evolution GO WhatsApp API
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_GROUP}
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${BINARY}
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
UMask=027

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager || true

log "Installation complete. Use 'journalctl -u ${SERVICE_NAME} -f' to follow logs."
