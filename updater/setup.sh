#!/bin/sh
set -eu

log() { printf '[updater-setup] %s\n' "$*" >&2; }

move_path() (
  source_path="$1"
  destination_path="$2"
  if [ -d "$source_path" ] && [ ! -L "$source_path" ]; then
    mkdir -p "$destination_path"
    for child_path in "$source_path"/* "$source_path"/.[!.]* "$source_path"/..?*; do
      [ -e "$child_path" ] || [ -L "$child_path" ] || continue
      move_path "$child_path" "$destination_path/${child_path##*/}"
    done
    rmdir "$source_path" 2>/dev/null || true
    return
  fi
  rm -f "$destination_path"
  mv "$source_path" "$destination_path"
)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR=/etc/systemd/system
SRC_DIR="$SCRIPT_DIR/systemd"
INSTALL_ROOT="${VO_INSTALL_ROOT:-/opt/vehicle-overseer}"

UPDATER_SERVICE_NAME=vehicle-overseer-updater.service
UPDATER_TIMER_NAME=vehicle-overseer-updater.timer

NO_SYSTEMD=0
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  NO_SYSTEMD=1
  log "systemd not running; will copy unit files but skip systemctl"
fi

CURRENT_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$INSTALL_ROOT/state.json" 2>/dev/null | head -n 1)"
MIGRATION_DIR="$INSTALL_ROOT/.migration020"
if [ "$SCRIPT_DIR" = "$INSTALL_ROOT" ] && [ -d "$MIGRATION_DIR" ]; then
  rmdir "$MIGRATION_DIR"
elif [ "$SCRIPT_DIR" = "$INSTALL_ROOT/app" ]; then
  case "$CURRENT_VERSION" in
    v0.0.*|v0.1.*)
      log "migrate pre-v0.2.0 nested artifact layout"
      mkdir "$MIGRATION_DIR"
      mv "$SCRIPT_DIR" "$MIGRATION_DIR/app"
      move_path "$MIGRATION_DIR/app" "$INSTALL_ROOT"
      cd "$INSTALL_ROOT"
      log "migration complete; update restarted"
      exec sh "$INSTALL_ROOT/update.sh"
      ;;
  esac
fi

if [ ! -d "$SRC_DIR" ]; then
  log "no updater systemd templates found"
  exit 0
fi

mkdir -p "$SYSTEMD_DIR"
mkdir -p /usr/local/bin
ln -sfnT "$INSTALL_ROOT/updater.py" /usr/local/bin/vehicle-overseer-updater.py

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
