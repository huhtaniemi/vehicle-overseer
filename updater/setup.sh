#!/bin/sh
set -eu

log() { printf '[updater-setup] %s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR=/etc/systemd/system
SRC_DIR="$SCRIPT_DIR/systemd"

UPDATER_SERVICE_NAME=vehicle-overseer-updater.service
UPDATER_TIMER_NAME=vehicle-overseer-updater.timer

NO_SYSTEMD=0
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  NO_SYSTEMD=1
  log "systemd not running; will copy unit files but skip systemctl"
fi

if [ ! -d "$SRC_DIR" ]; then
  log "no updater systemd templates found"
  exit 0
fi

mkdir -p "$SYSTEMD_DIR"

if [ -f "$SRC_DIR/updater.service" ]; then
  cp "$SRC_DIR/updater.service" "$SYSTEMD_DIR/$UPDATER_SERVICE_NAME"
  chmod 0644 "$SYSTEMD_DIR/$UPDATER_SERVICE_NAME"
else
  log "warn: missing updater.service in $SRC_DIR"
fi

if [ -f "$SRC_DIR/updater.timer" ]; then
  cp "$SRC_DIR/updater.timer" "$SYSTEMD_DIR/$UPDATER_TIMER_NAME"
  chmod 0644 "$SYSTEMD_DIR/$UPDATER_TIMER_NAME"
else
  log "warn: missing updater.timer in $SRC_DIR"
fi

if [ "$NO_SYSTEMD" -eq 0 ]; then
  systemctl daemon-reload || log "warn: systemctl daemon-reload failed"
  systemctl enable --now "$UPDATER_TIMER_NAME" || log "warn: failed to enable $UPDATER_TIMER_NAME"
else
  log "systemd unavailable; unit files copied but not enabled"
fi

log "ok"
