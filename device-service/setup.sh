#!/bin/sh
set -eu

log() { printf '[device-setup] %s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR=/etc/systemd/system
UNIT_SRC="$SCRIPT_DIR/systemd/vehicle-overseer.service"
UNIT_DST="$SYSTEMD_DIR/vehicle-overseer.service"

NO_SYSTEMD=0
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
	NO_SYSTEMD=1
	log "systemd not running; will copy unit file but skip systemctl"
fi

if [ ! -f "$UNIT_SRC" ]; then
	log "missing unit template: $UNIT_SRC"
	exit 2
fi

if [ ! -d "$SYSTEMD_DIR" ]; then
	log "missing systemd directory: $SYSTEMD_DIR"
	exit 2
fi

cp "$UNIT_SRC" "$UNIT_DST"
chmod 0644 "$UNIT_DST"

if [ "$NO_SYSTEMD" -eq 0 ]; then
	systemctl daemon-reload || log "warn: systemctl daemon-reload failed"
	if systemctl enable vehicle-overseer.service; then
		systemctl restart vehicle-overseer.service || log "warn: failed to restart vehicle-overseer.service"
	else
		log "warn: failed to enable vehicle-overseer.service"
	fi
else
	log "systemd unavailable; unit file copied but not enabled"
fi

log "ok"
