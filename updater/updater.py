#!/usr/bin/env python3
"""Pull-based updater.

Intended usage:
- Runs as a systemd timer (oneshot) as root.
- Pulls a per-device manifest from the backend and applies updates atomically.
- Executes update.sh from the artifact.

Backend endpoints used:
- GET  /api/device/manifest?uid=UID
- GET  /api/device/artifacts/<artifact-id>

Security note:
- the artifact bytes are decrypted
- the inner tar.gz hash is checked against the packaged `hash` file from the outer tar
- and the download size is validated when provided.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import urllib.error


def log(msg: str) -> None:
    print(f"[updater] {msg}")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _http_get_bytes(url: str) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        headers = {k: v for k, v in resp.headers.items()}
        return data, headers


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_tar_member_bytes(tar_path: str, member_name: str, mode: str = "r:*") -> bytes | None:
    with tarfile.open(tar_path, mode) as tar:
        try:
            member = tar.getmember(member_name)
        except KeyError:
            return None
        name = member.name
        if name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"unsafe tar path: {name!r}")
        norm = os.path.normpath(name)
        if norm.startswith("..") or os.path.isabs(norm):
            raise ValueError(f"unsafe tar path: {name!r}")
        if not member.isfile():
            raise ValueError(f"unexpected tar member type for {name!r}")
        fh = tar.extractfile(member)
        if fh is None:
            return None
        return fh.read()

def _read_state(path: str) -> dict | None:
    raw = _read_text(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _load_artifact_key(path: str | None) -> bytes | None:
    if not path:
        return None
    raw = _read_text(path)
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid base64 in artifact key file {path!r}: {exc}") from exc
    if len(key) != 32:
        raise ValueError(f"artifact key must be 32 bytes (got {len(key)})")
    return key


def _decrypt_aes_256_ctr(ciphertext: bytes, key: bytes, iv_hex: str) -> bytes:
    try:
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-ctr", "-K", key.hex(), "-iv", iv_hex],
            input=ciphertext,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("openssl not found; required for encrypted artifact downloads") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ValueError(f"artifact decrypt failed: {detail or 'decrypt failed'}")
    return proc.stdout


def _safe_extract_tar(data: bytes, dest_dir: str, mode: str = "r:gz") -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or name.startswith("\\"):
                raise ValueError(f"unsafe tar path: {name!r}")
            norm = os.path.normpath(name)
            if norm.startswith("..") or os.path.isabs(norm):
                raise ValueError(f"unsafe tar path: {name!r}")
            if member.issym() or member.islnk():
                raise ValueError(f"unsafe tar member (links not allowed): {name!r}")
            if member.isdev():
                raise ValueError(f"unsafe tar member (device file not allowed): {name!r}")
        try:
            tar.extractall(dest_dir, filter="data")
        except TypeError:
            tar.extractall(dest_dir)


def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], check=True, text=True, capture_output=True)


UPDATE_SCRIPT_NAME = "update.sh"


def _run_update_script(staging_dir: str, cwd: str, env: dict[str, str]) -> None:
    script_path = os.path.join(staging_dir, UPDATE_SCRIPT_NAME)
    if not os.path.isfile(script_path):
        raise ValueError(f"missing {UPDATE_SCRIPT_NAME} in artifact")
    if os.access(script_path, os.X_OK):
        cmd = [script_path]
    else:
        shell = shutil.which("bash") or shutil.which("sh")
        if not shell:
            raise RuntimeError("no shell found to run update.sh (install bash or sh)")
        cmd = [shell, script_path]
    log(f"run {UPDATE_SCRIPT_NAME}")
    proc = subprocess.run(cmd, cwd=cwd, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{UPDATE_SCRIPT_NAME} failed (exit {proc.returncode})")


def _record_failed_artifact(state_path: str, artifact_id: str) -> None:
    state = _read_state(state_path)
    if not isinstance(state, dict):
        state = {}
    blacklisted = state.get("blacklisted")
    if not isinstance(blacklisted, list):
        blacklisted = []
    if artifact_id not in blacklisted:
        blacklisted.append(artifact_id)
    state["blacklisted"] = blacklisted
    _write_text(state_path, json.dumps(state, indent=2) + "\n")


def cmd_apply(args: argparse.Namespace) -> int:
    backend = args.backend.rstrip("/")
    install_root = args.install_root
    artifact_key = _load_artifact_key(args.artifact_key_path or _env("VO_ARTIFACT_KEY_PATH"))
    if artifact_key is None:
        raise ValueError("artifact key required (artifacts are always encrypted)")
    log(f"backend={backend} installRoot={install_root}")
    uid_path = (
        args.device_uid_path
        or _env("VO_DEVICE_UID_PATH")
        or "/etc/vehicle-overseer/device.uid"
    )
    device_uid = args.device_uid or _read_text(uid_path)
    if artifact_key is not None and not device_uid:
        raise ValueError("artifact key configured but no device UID available")
    log(f"deviceUidPath={uid_path}")
    if not device_uid:
        raise ValueError("device UID required to fetch manifest")

    state_path = os.path.join(install_root, "state.json")
    state = _read_state(state_path)
    if not isinstance(state, dict):
        state = {}
    manifest_query = {"uid": device_uid}
    blacklisted = state.get("blacklisted")
    if isinstance(blacklisted, list) and blacklisted:
        manifest_query["blacklisted"] = json.dumps(blacklisted)
    manifest_url = f"{backend}/api/device/manifest?{urllib.parse.urlencode(manifest_query)}"
    log(f"fetch manifest: {manifest_url}")
    try:
        manifest = _http_get_json(manifest_url)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        msg = f"HTTP {exc.code}" + (f" {exc.reason}" if exc.reason else "")
        log(f"{msg}: {body}" if body else msg)
        if exc.code == 404:
            return 0
        raise

    version = manifest.get("version")
    artifact = manifest.get("artifact")
    if not version or not artifact:
        log("no artifact assigned for device")
        return 0

    artifact_url = artifact.get("url")
    artifact_id = artifact.get("id")
    artifact_size_bytes = artifact.get("sizeBytes")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest missing version")
    if not isinstance(artifact_url, str) or not artifact_url:
        raise ValueError("manifest missing artifact.url")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("manifest missing artifact.id")
    if artifact_size_bytes is not None and not isinstance(artifact_size_bytes, int):
        artifact_size_bytes = None

    current_artifact_id = state.get("artifactId") if state else None
    if not args.force and current_artifact_id == artifact_id:
        log(f"artifact {artifact_id} already installed; skipping")
        return 0

    full_artifact_url = urllib.parse.urljoin(f"{backend}/", artifact_url.lstrip("/"))
    sep = "&" if "?" in full_artifact_url else "?"
    full_artifact_url = f"{full_artifact_url}{sep}uid={device_uid}"
    log(f"download artifact: {full_artifact_url}")
    blob, headers = _http_get_bytes(full_artifact_url)
    enc = headers.get("X-VO-Enc") or headers.get("x-vo-enc")
    if not enc:
        raise ValueError("artifact response not encrypted")
    if enc.strip().lower() != "aes-256-ctr":
        raise ValueError(f"unsupported artifact encryption: {enc!r}")
    iv = headers.get("X-VO-Iv") or headers.get("x-vo-iv")
    if not iv:
        raise ValueError("encrypted artifact missing X-VO-Iv")
    blob = _decrypt_aes_256_ctr(blob, artifact_key, iv.strip())
    if artifact_size_bytes is not None and len(blob) != artifact_size_bytes:
        raise ValueError(
            f"artifact size mismatch: expected {artifact_size_bytes}, got {len(blob)}"
        )

    os.makedirs(install_root, exist_ok=True)
    artifact_path = os.path.join(install_root, artifact_id)
    try:
        with open(artifact_path, "wb") as f:
            f.write(blob)

        # Read packaged hash and inner payload (outer tar contains ./hash and ./data).
        hash_file_bytes = _read_tar_member_bytes(artifact_path, "./hash", "r:*")
        if not hash_file_bytes:
            raise ValueError("artifact missing hash file")
        inner_bytes = _read_tar_member_bytes(artifact_path, "./data", "r:*")
        if not inner_bytes:
            raise ValueError("artifact missing data file")

        hash_file_val = hash_file_bytes.decode("utf-8", errors="replace").strip()
        inner_sha = _sha256_hex(inner_bytes)
        if inner_sha != hash_file_val:
            raise ValueError(
                f"artifact hash mismatch: payload {inner_sha}, hash file {hash_file_val}"
            )

        log("extract artifact")
        _safe_extract_tar(inner_bytes, install_root, "r:gz")
        if not os.path.isfile(os.path.join(install_root, UPDATE_SCRIPT_NAME)):
            raise ValueError(f"{UPDATE_SCRIPT_NAME} missing from artifact")

        env = os.environ.copy()
        env["VO_INSTALL_ROOT"] = install_root
        env["VO_BACKEND"] = backend
        _run_update_script(install_root, install_root, env)

        state = _read_state(state_path)
        if not isinstance(state, dict):
            state = {}
        state["uid"] = device_uid
        state["version"] = version
        state["artifactId"] = artifact_id
        state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_text(state_path, json.dumps(state, indent=2) + "\n")
        log("update complete")
    except Exception:
        _record_failed_artifact(state_path, artifact_id)
        raise
    finally:
        if os.path.exists(artifact_path):
            os.remove(artifact_path)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default=_env("VO_BACKEND", "http://localhost:3100"), help="Backend base URL")
    parser.add_argument(
        "--install-root",
        default=_env("VO_INSTALL_ROOT", "/opt/vehicle-overseer"),
        help="Install root directory",
    )
    parser.add_argument("--force", action="store_true", help="Apply even if artifact matches current")
    parser.add_argument(
        "--artifact-key-path",
        default=None,
        help="Path to base64 artifact key (or set VO_ARTIFACT_KEY_PATH)",
    )
    parser.add_argument(
        "--device-uid",
        default=None,
        help="Device UID for encrypted artifact downloads (explicit override)",
    )
    parser.add_argument(
        "--device-uid-path",
        default=_env("VO_DEVICE_UID_PATH"),
        help="Path to device UID file (default: /etc/vehicle-overseer/device.uid)",
    )

    args = parser.parse_args()
    raise SystemExit(cmd_apply(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # show a clean fatal message instead of a Python trace
        log(f"error: {exc}")
        sys.exit(1)
