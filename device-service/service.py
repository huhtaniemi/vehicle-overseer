#!/usr/bin/env python3
"""
Single-device pinger service.

- Waits for network/VPN interface (default: tun0) to have an IPv4 address.
- Posts periodic status pings to the backend (/api/ping).
- Listens for per-action connections from the backend (backend -> device) and returns
  only final success or error.
- Exposes a service log stream endpoint that the backend can proxy to the UI.
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


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _iface_ipv4_addrs(iface: str) -> list[ipaddress.IPv4Interface]:
    out = subprocess.run(["ip", "-o", "-f", "inet", "addr", "show", "dev", iface], check=True, text=True, capture_output=True).stdout
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


class DeviceRuntime:
    def __init__(self, stop_event: threading.Event, retry_s: float) -> None:
        self.stop_event = stop_event
        self.stopping = threading.Event()
        self.retry_delay_s = retry_s
        self.bind_host: str | None = None


class Device:
    def __init__(
        self,
        runtime: DeviceRuntime,
        uid: str,
        label: str,
        backend_base: str,
        bind_host: str,
        ping_interval_s: float,
        report_iface: str | None,
        wait_timeout_s: float,
    ) -> None:
        self._runtime = runtime
        self._threads: list[threading.Thread] = []
        self._listeners: list[TcpService] = []
        self._service_version = self._read_service_version()
        self.uid = uid
        self.label = label
        self.backend_base = backend_base.rstrip("/")
        self.reported_ip: str | None = None
        self.bind_host = bind_host
        self.ping_interval_s = ping_interval_s
        self.report_iface = report_iface
        self.wait_timeout_s = wait_timeout_s

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

    def start(self) -> None:
        if self.report_iface:
            self.reported_ip = wait_for_iface_ipv4(self.report_iface, self.wait_timeout_s)
        self._runtime.bind_host = self._effective_bind_host()
        for listener in self._listeners:
            listener.ensure_started(force=True)
        self._start_thread(self.post_ping_loop)
        self._start_thread(self.listener_supervisor_loop)

    def add_listener(self, listener: TcpService) -> None:
        self._listeners.append(listener)

    def _start_thread(self, target) -> None:  # type: ignore[no-untyped-def]
        thread = threading.Thread(target=target, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _bind_host_is_dynamic(self) -> bool:
        return self.bind_host in {"", "auto", "reported"}

    def _effective_bind_host(self) -> str | None:
        if self._bind_host_is_dynamic():
            return self.reported_ip
        return self.bind_host

    def _refresh_reported_ip_if_needed(self) -> None:
        if not self.report_iface:
            return
        current = _iface_first_ipv4(self.report_iface)
        if current == self.reported_ip:
            return

        old_ip = self.reported_ip
        self.reported_ip = current
        self._runtime.bind_host = self._effective_bind_host()
        if not self._bind_host_is_dynamic():
            return
        if current is None:
            print(f"[net] {self.report_iface!r} lost IPv4 address")
            for listener in self._listeners:
                listener.stop()
            return
        if old_ip is None:
            print(f"[net] {self.report_iface!r} IPv4 acquired {current}; starting TCP servers")
        else:
            print(f"[net] {self.report_iface!r} IPv4 changed {old_ip} -> {current}; rebinding TCP servers")
        for listener in self._listeners:
            listener.restart()

    def listener_supervisor_loop(self) -> None:
        while not self._runtime.stop_event.wait(1.0):
            for listener in self._listeners:
                listener.ensure_started()

    def shutdown(self) -> None:
        if self._runtime.stopping.is_set():
            return
        self._runtime.stopping.set()
        self._runtime.stop_event.set()
        for listener in self._listeners:
            listener.stop()
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(timeout=2)

    def post_ping_loop(self) -> None:
        last_wait_log = 0.0
        while not self._runtime.stop_event.is_set():
            if self.report_iface:
                self._refresh_reported_ip_if_needed()
                if not self.reported_ip:
                    now = time.monotonic()
                    if now - last_wait_log >= 5:
                        print(f"[net] {self.report_iface!r} has no IPv4 address yet; delaying POST ping")
                        last_wait_log = now
                    if self._runtime.stop_event.wait(1.0):
                        break
                    continue

            data: dict[str, object] = {
                "version": {
                    "serviceVersion": self._service_version,
                },
            }
            for listener in self._listeners:
                data[listener.kind+"Port"] = listener.port

            payload = {
                "uid": self.uid,
                "label": self.label,
                "ip-address": self.reported_ip,
                "state": "not implemented",
                "data": data,
            }

            try:
                post_json(f"{self.backend_base}/api/ping", payload)
            except Exception as exc:
                print(f"[{self.label}] ping failed: {exc}")
            if self._runtime.stop_event.wait(self.ping_interval_s):
                break


class _TcpRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.server.service.handle_client(self.request)  # type: ignore[attr-defined]


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: "TcpService") -> None:
        self.service = service
        super().__init__(address, _TcpRequestHandler)


class TcpService:
    def __init__(self, runtime: DeviceRuntime, kind: str, port: int) -> None:
        self.runtime = runtime
        self.kind = kind
        self.port = port
        self._server: _ThreadingTcpServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._retry_at = 0.0

    def _delay_retry(self, now: float | None = None) -> None:
        self._retry_at = (now if now is not None else time.monotonic()) + self.runtime.retry_delay_s

    def ensure_started(self, *, force: bool = False) -> bool:
        bind_ip = self.runtime.bind_host
        if not bind_ip:
            return False
        now = time.monotonic()
        with self._lock:
            if self.runtime.stopping.is_set():
                return False
            if self._server is not None:
                return True
            if not force and now < self._retry_at:
                return False
            try:
                server = _ThreadingTcpServer((bind_ip, self.port), self)
            except OSError as exc:
                self._delay_retry(now)
                print(f"[net] failed to bind {self.kind} TCP server on {bind_ip}:{self.port}: {exc}; retrying in {int(self.runtime.retry_delay_s)}s")
                return False
            thread = threading.Thread(target=self._serve, args=(server,), daemon=True)
            self._server = server
            self._thread = thread
            self._retry_at = 0.0
        try:
            thread.start()
        except RuntimeError as exc:
            with self._lock:
                if self._server is server:
                    self._server = None
                    self._thread = None
                self._delay_retry()
            server.server_close()
            print(f"[net] failed to start {self.kind} TCP server thread: {exc}; retrying in {int(self.runtime.retry_delay_s)}s")
            return False
        return True

    def _serve(self, server: _ThreadingTcpServer) -> None:
        unexpected = False
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as exc:
            unexpected = True
            print(f"[net] {self.kind} TCP server crashed: {exc}")
        finally:
            should_retry = False
            with self._lock:
                if self._server is server:
                    self._server = None
                    self._thread = None
                if unexpected and not self.runtime.stopping.is_set():
                    self._delay_retry()
                    should_retry = True
            server.server_close()
            if should_retry:
                print(f"[net] {self.kind} TCP server stopped; retrying in {int(self.runtime.retry_delay_s)}s")

    def restart(self) -> None:
        self.stop()
        self.ensure_started(force=True)

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def handle_client(self, client: socket.socket) -> None:
        raise NotImplementedError()


class ActionTcpService(TcpService):
    def __init__(self, runtime: DeviceRuntime, port: int, jsonpath: str, mqtt_key: str, service_name: str) -> None:
        super().__init__(runtime, "action", port)
        self.jsonpath = jsonpath
        self.mqtt_key = mqtt_key
        self.service_name = service_name
        self._action_lock = threading.Lock()

    def _replace_mqtt_value(self, current: str, requested_ip: str) -> str:
        requested = requested_ip.strip()
        if not requested or "://" in requested:
            return requested
        if ":" in requested:
            scheme = re.match(r"^[^:]+://", current)
            return f"{scheme.group(0)}{requested}" if scheme else requested
        match = re.match(r"^(?P<prefix>[^:]+://)?(?P<host>[^:/]+)(?P<suffix>.*)$", current)
        return f"{match.group('prefix') or ''}{requested}{match.group('suffix')}" if match else requested

    def _find_key_paths(self, data: object, key: str) -> list[list[str]]:
        paths: list[list[str]] = []

        def walk(obj: object, prefix: list[str]) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    path = prefix + [k]
                    if k == key:
                        paths.append(path)
                    walk(v, path)
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
                data = json.load(f)
        except FileNotFoundError as exc:
            raise ValueError(f"{self.jsonpath} not found!") from exc
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

        with open(self.jsonpath, "r+", encoding="utf-8") as f:
            f.seek(0)
            json.dump(data, f, indent=2)
            f.write("\n")
            f.truncate()
        return new_value

    def handle_action(self, requested_ip: str) -> dict:
        if not requested_ip:
            return {"ok": False, "error": "missing ip"}
        with self._action_lock:
            try:
                new_value = self._update_properties_json(requested_ip)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            if not self.service_name:
                return {"ok": True, "key": self.mqtt_key, "value": new_value, "restarted": False}
            try:
                subprocess.run(["systemctl", "restart", self.service_name], check=True, timeout=10)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                return {"ok": False, "error": f"service restart failed: {exc}"}
            return {"ok": True, "key": self.mqtt_key, "value": new_value, "restarted": True}

    def handle_client(self, client: socket.socket) -> None:
        stream = client.makefile("rwb")
        raw = stream.readline().decode("utf-8", errors="replace").strip()
        try:
            msg = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            out = {"ok": False, "error": "invalid json"}
            stream.write((json.dumps(out) + "\n").encode("utf-8"))
            stream.flush()
            return
        requested_ip = msg.get("ip", "")
        print(f"[action] received: ip={requested_ip!r}")
        out = self.handle_action(requested_ip)
        print(f"[action] result: {out}")
        stream.write((json.dumps(out) + "\n").encode("utf-8"))
        stream.flush()


class LogTcpService(TcpService):
    def __init__(self, runtime: DeviceRuntime, port: int, service_name: str, service_log_since: str) -> None:
        super().__init__(runtime, "log", port)
        self.service_name = service_name.strip()
        self.service_log_since = service_log_since.strip()

    def handle_client(self, client: socket.socket) -> None:
        peer = f"{client.getpeername()[0]}:{client.getpeername()[1]}"
        print(f"[log] client connected: {peer}")
        journalctl_proc = None
        try:
            cmd = ["journalctl", "-f", "--output=cat", "--no-pager"]
            if self.service_name:
                cmd.extend(["-u", self.service_name])
            if self.service_log_since:
                cmd.extend(["--since", self.service_log_since])
            journalctl_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert journalctl_proc.stdout is not None
            stdout_fd = journalctl_proc.stdout.fileno()
            peek_flags = socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)
            while True:
                if journalctl_proc.poll() is not None:
                    break
                ready, _, _ = select.select([stdout_fd], [], [], 1.0)
                if ready:
                    chunk = os.read(stdout_fd, 4096)
                    if not chunk:
                        break
                    try:
                        client.sendall(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                try:
                    peek = client.recv(1, peek_flags)
                    if peek == b"":
                        break
                except BlockingIOError:
                    continue
                except (ConnectionResetError, OSError):
                    break
        except FileNotFoundError:
            client.sendall(b"[log] journalctl not found\n")
        finally:
            if journalctl_proc and journalctl_proc.poll() is None:
                journalctl_proc.terminate()
                try:
                    journalctl_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    journalctl_proc.kill()
                    journalctl_proc.wait(timeout=2)
            print(f"[log] client disconnected: {peer}")


def cmd_run(args: argparse.Namespace) -> int:
    global _iface_first_ipv4

    stop_event = threading.Event()
    runtime = DeviceRuntime(stop_event, 60.0)

    def _handle_exit(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        del signum
        stop_event.set()

    device_uid = args.uid or _read_text(args.uid_path)
    if not device_uid:
        print("Device UID is required")
        return 2

    label = args.label
    if not args.jsonpath:
        print("JSON path is required")
        return 2
    jsonpath = args.jsonpath
    mqtt_key = args.mqtt_key
    print(
        f"Device starting uid={device_uid!r} label={label!r} -> {args.backend} (iface {args.report_iface}, bind {args.bind_host}, jsonpath {jsonpath})"
    )

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    if args.report_ip_override:
        report_ip_override = str(ipaddress.ip_interface(args.report_ip_override).ip)
        _iface_first_ipv4 = lambda iface: report_ip_override

    device = Device(
        runtime=runtime,
        uid=device_uid,
        label=label,
        backend_base=args.backend,
        bind_host=args.bind_host,
        ping_interval_s=args.ping_interval_s,
        report_iface=args.report_iface,
        wait_timeout_s=args.wait_timeout_s,
    )

    device.add_listener(ActionTcpService(runtime, args.action_port, jsonpath, mqtt_key, args.service_name))
    device.add_listener(LogTcpService(runtime, args.log_port, args.service_name, args.service_log_since))

    try:
        device.start()
        stop_event.wait()
    except TimeoutError as exc:
        print(f"[net] {exc}")
        return 2
    finally:
        device.shutdown()
    print("Device exiting")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", default=os.environ.get("VO_BACKEND") or "http://localhost:3100", help="Backend base URL")
    common.add_argument("--uid", default=None, help="Device UID to report, explicit override")
    common.add_argument(
        "--uid-path",
        default=os.environ.get("VO_DEVICE_UID_PATH", "/etc/vehicle-overseer/device.uid"),
        help="Path to device UID file",
    )
    common.add_argument("--label", default=os.environ.get("VO_LABEL"), help="Display label for UI/logs")
    common.add_argument(
        "--service-name",
        default=os.environ.get("VO_SERVICE_NAME") or "",
        help="Systemd unit name for action restart and optional service-log filtering",
    )
    common.add_argument("--action-port", type=int, default=int(os.environ.get("VO_ACTION_PORT") or 9000), help="TCP port for action endpoint")
    common.add_argument("--log-port", type=int, default=int(os.environ.get("VO_LOG_PORT") or 9100), help="TCP port for service log endpoint")
    common.add_argument(
        "--service-log-since",
        default=os.environ.get("VO_SERVICE_LOG_SINCE") or "",
        help="journalctl --since value for service log stream history",
    )
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
    common.add_argument(
        "--report-ip-override",
        default=None,
        help="Override reported ip-address for test environments",
    )
    common.add_argument(
        "--wait-timeout-s",
        type=float,
        default=float(os.environ.get("VO_WAIT_TIMEOUT_S") or 0.0),
        help="Seconds to wait for report-iface to get an IPv4 address (0 = forever)",
    )
    common.add_argument(
        "--ping-interval-s",
        type=float,
        default=float(os.environ.get("VO_PING_INTERVAL_S") or 10.0),
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
