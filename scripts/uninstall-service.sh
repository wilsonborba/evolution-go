#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="evolution-go"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

log() {
  printf '[evolution-go-uninstall] %s\n' "$1"
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

log "Uninstall complete. Database, sessions, and the project clone were left untouched."
