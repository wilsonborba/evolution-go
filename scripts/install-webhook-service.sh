#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SERVICE_NAME="evolution-go-webhook"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="${SUDO_USER:-$(id -un)}"
CURRENT_GROUP="$(id -gn "$CURRENT_USER")"

log() {
  printf '[evolution-go-webhook-install] %s\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

require_command sudo
require_command systemctl
require_command python3

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "No .env found at $ROOT_DIR/.env. The listener reads GLOBAL_API_KEY and POSTGRES_USERS_DB from there." >&2
  exit 1
fi

LISTENER_DIR="${ROOT_DIR}/tools/webhook-listener"
VENV_DIR="${LISTENER_DIR}/.venv"

if [ ! -x "${VENV_DIR}/bin/python3" ] || ! "${VENV_DIR}/bin/python3" -c "import psycopg2" >/dev/null 2>&1; then
  log "No working .venv found (missing or psycopg2 not importable on this host), (re)creating it."
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
  "${VENV_DIR}/bin/pip" install --quiet psycopg2-binary
  "${VENV_DIR}/bin/python3" -c "import psycopg2" || { echo "psycopg2 still not importable after a fresh venv -- stopping." >&2; exit 1; }
fi

PYTHON_BIN="${VENV_DIR}/bin/python3"

log "Installing systemd unit for ${SERVICE_NAME} (WorkingDirectory=${ROOT_DIR}, ExecStart=${PYTHON_BIN} tools/webhook-listener/receiver.py)."
log "Binds 0.0.0.0 on an auto-discovered free port (default range starts at 4001) -- reachable from the whole LAN, not just localhost."
log "Events are stored in Postgres (webhook_events table, same DB as POSTGRES_USERS_DB), not in a local file."

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Evolution GO webhook receiver (auto port, LAN-wide)
After=network.target evolution-go.service
Wants=evolution-go.service

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_GROUP}
WorkingDirectory=${ROOT_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${ROOT_DIR}/tools/webhook-listener/receiver.py
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
log "Chosen port persists at ${ROOT_DIR}/tools/webhook-listener/.port"
log "Events land in the 'webhook_events' table on POSTGRES_USERS_DB."
