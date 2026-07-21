#!/usr/bin/env python3
"""
Single-device pinger service.

- Waits for network/VPN interface (default: tun0) to have an IPv4 address.
- Posts periodic status pings to the backend (/api/ping).
- Listens for per-action TCP connections from the backend (backend -> device) and returns
  only final success or error.
- Exposes a TCP log stream endpoint that the backend can proxy to the UI.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import os
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request


def post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _iface_ipv4_addrs(iface: str) -> list[ipaddress.IPv4Interface]:
    out = _run(["ip", "-o", "-f", "inet", "addr", "show", "dev", iface]).stdout
    addrs: list[ipaddress.IPv4Interface] = []
    for line in out.splitlines():
        m = re.search(r"\sinet\s+(\d+\.\d+\.\d+\.\d+/\d+)\s", line)
        if not m:
            continue
        addrs.append(ipaddress.ip_interface(m.group(1)))  # type: ignore[arg-type]
    return addrs


def _iface_first_ipv4(iface: str) -> str | None:
    try:
        addrs = _iface_ipv4_addrs(iface)
    except subprocess.CalledProcessError:
        return None
    for addr in addrs:
        ip = addr.ip
        if ip.is_loopback or ip.is_link_local:
            continue
        return str(ip)
    return str(addrs[0].ip) if addrs else None


def wait_for_iface_ipv4(iface: str, timeout_s: float) -> str:
    deadline = None if timeout_s <= 0 else (time.monotonic() + timeout_s)
    last_log = 0.0
    while True:
        ip = _iface_first_ipv4(iface)
        if ip:
            return ip

        now = time.monotonic()
        if now - last_log >= 5:
            print(f"[net] waiting for {iface!r} to have an IPv4 address...")
            last_log = now

        if deadline is not None and now >= deadline:
            raise TimeoutError(f"timed out waiting for {iface!r} to have an IPv4 address")
        time.sleep(1)


class Device:
    def __init__(
        self,
        uid: str,
        label: str,
        backend_base: str,
        reported_ip: str,
        bind_host: str,
        action_port: int,
        log_port: int,
        ping_interval_s: float,
        jsonpath: str,
        mqtt_key: str,
        report_iface: str | None,
        service_name: str,
    ) -> None:
        self.uid = uid
        self.label = label
        self.backend_base = backend_base.rstrip("/")
        self.reported_ip: str | None = reported_ip
        self.bind_host = bind_host
        self.action_port = action_port
        self.log_port = log_port
        self.ping_interval_s = ping_interval_s
        self.jsonpath = jsonpath
        self.mqtt_key = mqtt_key
        self.report_iface = report_iface
        self._action_count = 0
        self._lock = threading.Lock()
        self._service_version = self._read_service_version()
        self.service_name = service_name
        self._action_server: ThreadingTCPServer | None = None
        self._log_server: ThreadingTCPServer | None = None
        self._servers_lock = threading.Lock()

    def _read_service_version(self) -> str | None:
        base = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(base, "VERSION"), os.path.join(base, "version.txt")):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                return v or None
            except FileNotFoundError:
                continue
        return None

    def _refresh_reported_ip_if_needed(self) -> None:
        if not self.report_iface:
            return
        current = _iface_first_ipv4(self.report_iface)
        old_ip = self.reported_ip
        self.reported_ip = current

        if current != old_ip:
            if current is None:
                print(f"[net] {self.report_iface!r} lost IPv4 address; stopping TCP servers")
                self._stop_tcp_servers()
            else:
                if old_ip is not None:
                    print(f"[net] {self.report_iface!r} IPv4 changed {old_ip} -> {current}; rebinding TCP servers")
                else:
                    print(f"[net] {self.report_iface!r} IPv4 acquired {current}; starting TCP servers")
                self._restart_tcp_servers(current)

    def _start_tcp_servers(self, bind_ip: str) -> None:
        with self._servers_lock:
            try:
                action_server = ThreadingTCPServer((bind_ip, self.action_port), ActionTCPHandler)
                action_server.device = self  # type: ignore[attr-defined]
                log_server = ThreadingTCPServer((bind_ip, self.log_port), LogTCPHandler)
                log_server.device = self  # type: ignore[attr-defined]
            except OSError as exc:
                print(f"[net] failed to bind TCP servers on {bind_ip}:{self.action_port}/{self.log_port}: {exc}")
                return
            self._action_server = action_server
            self._log_server = log_server
            self.bind_host = bind_ip
            threading.Thread(target=action_server.serve_forever, daemon=True).start()
            threading.Thread(target=log_server.serve_forever, daemon=True).start()

    def _stop_tcp_servers(self) -> None:
        with self._servers_lock:
            if self._action_server:
                self._action_server.shutdown()
                self._action_server = None
            if self._log_server:
                self._log_server.shutdown()
                self._log_server = None

    def _restart_tcp_servers(self, bind_ip: str) -> None:
        self._stop_tcp_servers()
        self._start_tcp_servers(bind_ip)

    def post_ping_loop(self) -> None:
        last_wait_log = 0.0
        while True:
            if self.report_iface:
                self._refresh_reported_ip_if_needed()
                if not self.reported_ip:
                    now = time.monotonic()
                    if now - last_wait_log >= 5:
                        print(f"[net] {self.report_iface!r} has no IPv4 address yet; delaying POST ping")
                        last_wait_log = now
                    time.sleep(1)
                    continue

            payload = {
                "uid": self.uid,
                "label": self.label,
                "ip-address": self.reported_ip,
                "state": "not implemented",
                "data": {
                    "actionPort": self.action_port,
                    "logPort": self.log_port,
                    "version": {
                        "serviceVersion": self._service_version,
                    },
                },
            }

            try:
                post_json(f"{self.backend_base}/api/ping", payload)
            except Exception as exc:
                print(f"[{self.label}] ping failed: {exc}")
            time.sleep(self.ping_interval_s)

    def _replace_mqtt_value(self, current: str, requested_ip: str) -> str:
        requested = requested_ip.strip()
        if not requested:
            return requested
        if "://" in requested:
            return requested
        if ":" in requested:
            scheme = re.match(r"^[^:]+://", current)
            if scheme:
                return f"{scheme.group(0)}{requested}"
            return requested
        match = re.match(r"^(?P<prefix>[^:]+://)?(?P<host>[^:/]+)(?P<suffix>.*)$", current)
        if match:
            return f"{match.group('prefix') or ''}{requested}{match.group('suffix')}"
        return requested

    def _find_key_paths(self, data: object, key: str) -> list[list[str]]:
        paths: list[list[str]] = []

        def walk(obj: object, prefix: list[str]) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    next_prefix = prefix + [k]
                    if k == key:
                        paths.append(next_prefix)
                    walk(v, next_prefix)
            elif isinstance(obj, list):
                for idx, item in enumerate(obj):
                    walk(item, prefix + [str(idx)])

        walk(data, [])
        return paths

    def _get_by_path(self, data: object, path: list[str]) -> object:
        cur: object = data
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                raise ValueError(f"mqtt key path {'.'.join(path)!r} missing in {self.jsonpath}")
            cur = cur[key]
        return cur

    def _set_by_path(self, data: object, path: list[str], value: object) -> None:
        cur: object = data
        for key in path[:-1]:
            if not isinstance(cur, dict) or key not in cur:
                raise ValueError(f"mqtt key path {'.'.join(path)!r} missing in {self.jsonpath}")
            cur = cur[key]
        if not isinstance(cur, dict):
            raise ValueError(f"mqtt key path {'.'.join(path)!r} missing in {self.jsonpath}")
        cur[path[-1]] = value

    def _update_properties_json(self, requested_ip: str) -> str:
        try:
            with open(self.jsonpath, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError as exc:
            raise ValueError(f"{self.jsonpath} not found!") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self.jsonpath} is invalid json: {exc}") from exc

        if "." in self.mqtt_key:
            key_path = [p for p in self.mqtt_key.split(".") if p]
        else:
            paths = self._find_key_paths(data, self.mqtt_key)
            if not paths:
                raise ValueError(f"mqtt key {self.mqtt_key!r} missing in {self.jsonpath}")
            if len(paths) > 1:
                raise ValueError(f"mqtt key {self.mqtt_key!r} is ambiguous; use a dotted path")
            key_path = paths[0]
        current = self._get_by_path(data, key_path)
        if not isinstance(current, str):
            raise ValueError(f"mqtt key {self.mqtt_key!r} must be a string")

        new_value = self._replace_mqtt_value(current, requested_ip)
        self._set_by_path(data, key_path, new_value)
        new_raw = json.dumps(data, indent=2) + "\n"

        tmp_path = f"{self.jsonpath}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_raw)
        os.replace(tmp_path, self.jsonpath)
        return new_value

    def _restart_service(self) -> None:
        if not self.service_name:
            return
        subprocess.run(["systemctl", "restart", self.service_name], check=True, timeout=10)

    def handle_action(self, requested_ip: str) -> dict:
        if not requested_ip:
            return {"ok": False, "error": "missing ip"}
        with self._lock:
            self._action_count += 1
            try:
                new_value = self._update_properties_json(requested_ip)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            if self.service_name:
                try:
                    self._restart_service()
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    return {"ok": False, "error": f"service restart failed: {exc}"}
                restarted = True
            else:
                restarted = False

        return {"ok": True, "key": self.mqtt_key, "value": new_value, "restarted": restarted}


class ActionTCPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        device: Device = self.server.device  # type: ignore[attr-defined]
        raw = self.rfile.readline().decode("utf-8", errors="replace").strip()
        try:
            msg = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            out = {"ok": False, "error": "invalid json"}
            self.wfile.write((json.dumps(out) + "\n").encode("utf-8"))
            return
        requested_ip = msg.get("ip", "")
        print(f"[{device.label}] action received: ip={requested_ip!r}")
        out = device.handle_action(requested_ip)
        print(f"[{device.label}] action result: {out}")
        self.wfile.write((json.dumps(out) + "\n").encode("utf-8"))


class LogTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        device: Device = self.server.device  # type: ignore[attr-defined]
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"[{device.label}] logs client connected: {peer}")
        proc = None
        try:
            proc = subprocess.Popen(
                ["journalctl", "--since", "1 hour ago", "-f", "--output=cat", "--no-pager"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            peek_flags = socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)
            while True:
                if proc.poll() is not None:
                    break
                ready, _, _ = select.select([stdout_fd], [], [], 1.0)
                if ready:
                    chunk = os.read(stdout_fd, 4096)
                    if not chunk:
                        break
                    try:
                        self.request.sendall(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                try:
                    peek = self.request.recv(1, peek_flags)
                    if peek == b"":
                        break
                except BlockingIOError:
                    continue
                except (ConnectionResetError, OSError):
                    break
        except FileNotFoundError:
            self.request.sendall(b"[log] journalctl not found\n")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            print(f"[{device.label}] logs client disconnected: {peer}")


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_device(device: Device) -> None:
    if device.reported_ip:
        device._start_tcp_servers(device.bind_host)
    threading.Thread(target=device.post_ping_loop, daemon=True).start()


def _handle_exit(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
    raise KeyboardInterrupt


def cmd_run(args: argparse.Namespace) -> int:
    report_iface = None if args.report_ip else args.report_iface

    device_uid = args.uid or _read_text(args.uid_path)
    if not device_uid:
        print("Device UID is required")
        return 2

    if args.report_ip:
        reported_ip = args.report_ip
    else:
        try:
            reported_ip = wait_for_iface_ipv4(args.report_iface, args.wait_timeout_s)
        except TimeoutError as exc:
            print(f"[net] {exc}")
            return 2

    if args.bind_host in {"auto", "reported", ""}:
        bind_host = reported_ip
    else:
        bind_host = args.bind_host

    label = args.label
    if not args.jsonpath:
        print("JSON path is required")
        return 2
    jsonpath = args.jsonpath
    mqtt_key = args.mqtt_key
    print(
        f"Device starting uid={device_uid!r} label={label!r} -> {args.backend} (ip-address {reported_ip}, bind {bind_host}, jsonpath {jsonpath})"
    )

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    try:
        device = Device(
            uid=device_uid,
            label=label,
            backend_base=args.backend,
            reported_ip=reported_ip,
            bind_host=bind_host,
            action_port=args.action_port,
            log_port=args.log_port,
            ping_interval_s=args.ping_interval_s,
            jsonpath=jsonpath,
            mqtt_key=mqtt_key,
            report_iface=report_iface,
            service_name=args.service_name,
        )
        serve_device(device)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Device exiting")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", default=os.environ.get("VO_BACKEND") or "http://localhost:3100", help="Backend base URL")
    common.add_argument("--uid", default=os.environ.get("VO_DEVICE_UID"), help="Device UID to report")
    common.add_argument(
        "--uid-path",
        default=os.environ.get("VO_DEVICE_UID_PATH", "/etc/vehicle-overseer/device.uid"),
        help="Path to device UID file",
    )
    common.add_argument("--label", default=os.environ.get("VO_LABEL"), help="Display label for UI/logs")
    common.add_argument(
        "--service-name",
        default=os.environ.get("VO_SERVICE_NAME") or "",
        help="Optional systemd unit name to restart after applying selection (or set VO_SERVICE_NAME). If empty, restart is skipped.",
    )
    common.add_argument("--action-port", type=int, default=_env_int("VO_ACTION_PORT", 9000), help="TCP port for action endpoint")
    common.add_argument("--log-port", type=int, default=_env_int("VO_LOG_PORT", 9100), help="TCP port for log endpoint")
    common.add_argument(
        "--bind-host",
        default=os.environ.get("VO_BIND_HOST") or "auto",
        help="Host/IP to bind TCP servers on (auto = reported ip-address)",
    )
    common.add_argument(
        "--report-iface",
        default=os.environ.get("VO_REPORT_IFACE") or "tun0",
        help="Interface whose IPv4 address is reported as ip-address",
    )
    common.add_argument("--report-ip", default=None, help="Override reported ip-address (skips iface wait)")
    common.add_argument(
        "--wait-timeout-s",
        type=float,
        default=_env_float("VO_WAIT_TIMEOUT_S", 0.0),
        help="Seconds to wait for report-iface to get an IPv4 address (0 = forever)",
    )
    common.add_argument(
        "--ping-interval-s",
        type=float,
        default=_env_float("VO_PING_INTERVAL_S", 10.0),
        help="POST ping interval in seconds",
    )
    common.add_argument(
        "--jsonpath",
        default=os.environ.get("VO_JSONPATH"),
        help="Path to properties.json (required; set VO_JSONPATH)",
    )
    common.add_argument(
        "--mqtt-key",
        default=os.environ.get("VO_MQTT_KEY") or 'mqttServerIp',
        help="JSON key to update inside properties.json (default: mqttServerIp)",
    )

    p_run = sub.add_parser("run", help="Run device service", parents=[common])
    p_run.set_defaults(func=cmd_run)

    # Back-compat: allow running without explicit subcommand (treated as `run`)
    if len(sys.argv) >= 2 and sys.argv[1] != "run":
        sys.argv.insert(1, "run")
    if len(sys.argv) == 1:
        sys.argv.append("run")

    args = parser.parse_args()
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        raise SystemExit(2)
    raise SystemExit(func(args))


if __name__ == "__main__":
    main()
