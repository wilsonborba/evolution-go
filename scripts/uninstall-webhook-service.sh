#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SERVICE_NAME="evolution-go-webhook"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
EVOLUTION_BASE_URL="${EVOLUTION_BASE_URL:-http://127.0.0.1:4000}"
INSTANCE_NAME="${WEBHOOK_INSTANCE_NAME:-test01}"

log() {
  printf '[evolution-go-webhook-uninstall] %s\n' "$1"
}

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  log "Stopping ${SERVICE_NAME}."
  sudo systemctl stop "$SERVICE_NAME" || true
  log "Disabling ${SERVICE_NAME}."
  sudo systemctl disable "$SERVICE_NAME" || true
else
  log "${SERVICE_NAME}.service is not registered with systemd; nothing to stop/disable."
fi

if [ -f "$SERVICE_FILE" ]; then
  log "Removing unit file ${SERVICE_FILE}."
  sudo rm -f "$SERVICE_FILE"
  sudo systemctl daemon-reload
else
  log "Unit file ${SERVICE_FILE} not present."
fi

# Best-effort: clear the webhook registration on evolution-go so it stops
# trying to POST to a listener that's no longer running. Never fatal --
# evolution-go being offline or the instance being gone is fine here.
if [ -f "$ROOT_DIR/.env" ] && command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  GLOBAL_KEY=$(grep -m1 '^GLOBAL_API_KEY=' "$ROOT_DIR/.env" | cut -d '=' -f2- || true)
  if [ -n "${GLOBAL_KEY:-}" ]; then
    TOKEN=$(curl -s -m 5 "$EVOLUTION_BASE_URL/instance/all" -H "apikey: $GLOBAL_KEY" 2>/dev/null \
      | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin).get('data', [])
except Exception:
    data = []
match = [i for i in data if i.get('name') == '$INSTANCE_NAME']
print(match[0]['token'] if match else '')
" 2>/dev/null || true)
    if [ -n "$TOKEN" ]; then
      log "Clearing webhook registration on instance '${INSTANCE_NAME}'."
      curl -s -m 5 -X POST "$EVOLUTION_BASE_URL/instance/connect" \
        -H "Content-Type: application/json" -H "apikey: $TOKEN" \
        -d '{"webhookUrl": "", "subscribe": ["MESSAGE"]}' >/dev/null || true
    fi
  fi
fi

log "Uninstall complete. Captured events stay in the 'webhook_events' Postgres table (not deleted), and .venv/.port were left untouched."
