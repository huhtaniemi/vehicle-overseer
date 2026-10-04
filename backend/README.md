Backend scaffold
================

This is a Node.js backend for the web switcher concept. Implement the API/WebSocket/log streaming behavior per `architecture.txt` and `architecture.md`.

Setup
1) Create `config.json` (optional). If missing, the backend uses internal defaults.
2) Install deps: `npm install`.
3) Run: `npm start` (starts HTTP + WebSocket server; see endpoints below).

Example config.json
```json
{
	"dbPath": "./data/db",
	"httpHost": "0.0.0.0",
	"httpPort": 3100,
	"defaultSshUser": "user",
	"defaultServiceName": "usrapp.service",
	"defaultServiceLogSince": "5 minutes ago", // or ' -5m'
	"defaultMqttKey": "mqttServerIp",
	"deviceActionPort": 9000,
	"deviceLogPort": 9100,
	"devicePingIntervalS": 10,
	"ipList": [
		"tcp://10.99.2.10:11883",
		"tcp://10.99.12.10:11883",
		"tcp://10.102.1.10:11883"
	]
}
```

SEA binary build (official Node “Single Executable Application”)
1) Build on the target OS/arch (Node 20+ required for build time):
	- `npm install`
	- `npm run build:sea`
2) Deploy by copying `backend/dist/` to the target machine.
	- Run: `./vehicle-overseer-backend` from inside that folder.
	- Optional: create `config.json` next to the binary (otherwise it uses internal defaults).
	- Keep `dist/updater/` alongside the executable (used at runtime).

Artifacts
---------
Artifacts are hash-named outer tar files (containing `hash` + `data`/inner tar.gz).
- Build (from backend/): `node ../updater/artifacts.js <version> --module <path> [--module <path>]...` writes the artifact to `data/artifacts/<hash>` and prints the hash.
- Sync to DB (dev): `node src/index.js artifacts refresh` to synchronize artifact/version database entries with packages on disk and update `latest`.
- Server (SEA binary): copy the hash-named artifact into `data/artifacts/` and run `./vehicle-overseer-backend artifacts refresh`, or import directly: `./vehicle-overseer-backend artifacts import /path/to/<artifact-hash>`.

Notes:
- If the server is already running, the backend reads database records directly on subsequent HTTP requests.

Runtime root (SEA + dev)
- The backend resolves `config.json`, `data/`, and `updater/` relative to `process.cwd()`.
- For systemd, set `WorkingDirectory=/path/to/dist` (recommended).

Notes
- SEA produces a native executable by copying your current `node` binary and injecting an app blob.
- The backend will create `./data/` under the working directory on first run.

Key files
- `config.json`: Optional runtime config (if missing, backend uses internal defaults).
- `src/artifacts_cli.js`: Artifact manager module handling update artifacts/versions, per-device update targets, device keys, and bootstrap tokens.
- `src/index.js`: Minimal functional server with HTTP endpoints, shared WebSocket for UI, per-device log WebSocket proxy, database wiring for update metadata, and per-action backend→device TCP connections.
- `../device-service/simulator.py`: Python simulator that acts like a device/service (POST pings with uid + label, TCP action endpoint, TCP log stream).

The `cli` name remains from when it was a separate utility.

Endpoints
- HTTP: `GET /api/config`, `GET /api/entries`, `POST /api/ping`, `POST /api/action/select`, `GET /api/health`
- Updates: `GET /api/device/manifest`, `GET /api/device/artifacts/<id>`, `GET /api/device/key`
- Provisioning: `POST /api/bootstrap-token`, `GET /api/srvcsetup`, `GET /api/srvcsetup/files/<name>`
- Entry management: `POST /api/entries/clear`, `DELETE /api/entries/<uid>`
- WS: `ws://host:port/ws` (UI updates), `ws://host:port/logs?uid=UID` (per-device log proxy)

UI
- `GET /` serves `index.html` when available (so the backend can run standalone without Python).
- The SEA build does not copy frontend HTML into `dist/`; provide `index.html` separately to serve the UI.

Database
--------
Default layout (paths are relative to the backend working directory):
```text
data/artifacts/<filename>                 package bytes
data/db/artifacts/<id>                    -> ../../artifacts/<filename>
data/db/versions/<version>                -> ../artifacts/<id>
data/db/versions/latest                   -> ../artifacts/<id>
data/db/device_targets/<uid>              -> ../versions/<version>
data/db/device_keys/<uid>                 JSON: key_id, key_b64, created_at, updated_at
data/db/bootstrap_tokens/<token>          JSON: kind, created_at, used_at
```
- `dbPath` sets the database directory (default `./data/db`); packages remain in `data/artifacts/`.
- `latest` is selected by the embedded VERSION build timestamp, then descending version text for ties.
- Keys and tokens are private JSON files (mode 0600); record directories are created with mode 0700.
- Server and CLI writers share `.writer-lock`; competing writes fail with `database busy`.
- Each record is replaced atomically, but multi-record commits are not crash-atomic. Remove a stale lock only after stopping all writers.
