#!/bin/sh
set -eu

log() { printf '[srvcsetup] %s\n' "$*" >&2; }

trap 'rc=$?; if [ "$rc" -ne 0 ]; then log "ERROR (exit $rc) at line ${LINENO:-?}"; fi; exit $rc' EXIT

# This script is intended to be served by the backend at `/api/srvcsetup`.
# The backend replaces the __PLACEHOLDER__ values before returning it.

if [ "$(id -u)" -ne 0 ]; then
  log "run as root (use sudo)"
  exit 2
fi

BACKEND_BASE=__BACKEND_BASE__
LABEL=__LABEL__
TOKEN=__TOKEN__
REPORT_IFACE=__REPORT_IFACE__
ACTION_PORT=__ACTION_PORT__
LOG_PORT=__LOG_PORT__
PING_INTERVAL_S=__PING_INTERVAL_S__
INSTALL_ROOT=__INSTALL_ROOT__
ENV_DIR=__ENV_DIR__
SYSTEMD_DIR=/etc/systemd/system
UPDATER_SERVICE_NAME=vehicle-overseer-updater.service
UPDATER_TIMER_NAME=vehicle-overseer-updater.timer

if ! command -v systemctl >/dev/null 2>&1; then
  log "systemctl not found (systemd required)"
  exit 2
fi
if ! command -v tar >/dev/null 2>&1; then
  log "tar not found"
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "python3 not found"
  exit 2
fi

log "backend=$BACKEND_BASE label=$LABEL reportIface=$REPORT_IFACE actionPort=$ACTION_PORT logPort=$LOG_PORT pingInterval=${PING_INTERVAL_S}s"
log "installRoot=$INSTALL_ROOT envDir=$ENV_DIR"

mkdir -p "$INSTALL_ROOT" "$ENV_DIR"

FETCH() {
  url="$1"
  log "fetch: $url"
  curl -fsSL "$url"
}

# Fetch templates/config from backend (script stays minimal).
mkdir -p "$SYSTEMD_DIR"

log "fetch device artifact key (overwrite $ENV_DIR/artifact.key)"
if [ -z "$TOKEN" ]; then
  log "missing bootstrap token; call /api/srvcsetup with ?label=...&token=..."
  exit 3
fi
KEY_URL="$BACKEND_BASE/api/device/key?token=$TOKEN"
log "fetch: $KEY_URL"
KEY_RAW="$(curl -fsSL "$KEY_URL")"
NL='
'
DEVICE_UID=${KEY_RAW%%"$NL"*}
KEY_B64=${KEY_RAW#*"$NL"}
KEY_B64=${KEY_B64%%"$NL"*}
if [ -z "$KEY_B64" ] || [ -z "$DEVICE_UID" ]; then
  log "failed to parse key response"
  exit 3
fi
umask 077
printf "%s\n" "$KEY_B64" >"$ENV_DIR/artifact.key"
log "wrote $ENV_DIR/artifact.key"
printf "%s\n" "$DEVICE_UID" >"$ENV_DIR/device.uid"
log "wrote $ENV_DIR/device.uid"

if [ ! -f "$ENV_DIR/device.env" ]; then
  log "write $ENV_DIR/device.env (new)"
  FETCH "$BACKEND_BASE/api/srvcsetup/files/device.env?label=$LABEL&reportIface=$REPORT_IFACE&actionPort=$ACTION_PORT&logPort=$LOG_PORT&pingIntervalS=$PING_INTERVAL_S" >"$ENV_DIR/device.env"
else
  log "keep existing $ENV_DIR/device.env"
fi

if [ ! -f "$ENV_DIR/updater.env" ]; then
  log "write $ENV_DIR/updater.env (new)"
  FETCH "$BACKEND_BASE/api/srvcsetup/files/updater.env" >"$ENV_DIR/updater.env"
else
  log "keep existing $ENV_DIR/updater.env"
fi

if [ -t 1 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
  log "open editor to finalize config (device.env, updater.env)"
  if command -v nano >/dev/null 2>&1; then
    nano "$ENV_DIR/device.env" "$ENV_DIR/updater.env" </dev/tty >/dev/tty
  elif [ -n "${EDITOR:-}" ]; then
    "$EDITOR" "$ENV_DIR/device.env" "$ENV_DIR/updater.env" </dev/tty >/dev/tty
  else
    log "no editor available; edit $ENV_DIR/device.env and $ENV_DIR/updater.env if needed"
  fi
else
  log "no interactive TTY; skip editor (edit $ENV_DIR/device.env manually if needed)"
fi

. "$ENV_DIR/updater.env"
UPDATER_PATH="$VO_INSTALL_ROOT/updater.py"
mkdir -p "$VO_INSTALL_ROOT" /usr/local/bin
ln -sfnT "$UPDATER_PATH" /usr/local/bin/vehicle-overseer-updater.py

log "install bootstrap updater"
FETCH "$BACKEND_BASE/api/srvcsetup/files/updater.py" >"$UPDATER_PATH"
chmod +x "$UPDATER_PATH"

log "install updater systemd units"
FETCH "$BACKEND_BASE/api/srvcsetup/files/updater.service" >"$SYSTEMD_DIR/$UPDATER_SERVICE_NAME"
FETCH "$BACKEND_BASE/api/srvcsetup/files/updater.timer" >"$SYSTEMD_DIR/$UPDATER_TIMER_NAME"

log "enable updater timer"
systemctl daemon-reload
systemctl enable --now "$UPDATER_TIMER_NAME"

log "run updater (initial install)"
/usr/bin/python3 "$UPDATER_PATH" \
  --backend "$BACKEND_BASE" \
  --install-root "$VO_INSTALL_ROOT" \
  --force \
  --artifact-key-path "$ENV_DIR/artifact.key" \
  --device-uid-path "$ENV_DIR/device.uid"

log "ok"
