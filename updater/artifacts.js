#!/usr/bin/env node

// Artifact tooling.
//
// Creates a nested artifact:
//   outer tar (uncompressed)
//     - hash (SHA256 of inner tar.gz)
//     - data (payload tar.gz)
// Outputs to ./data/artifacts/<hash>.
//backend discovers artifacts via `refresh` command or on restart.
//
// Usage:
//   node updater/artifacts.js <version> --module <path> [--module <path>]... [--script <path>]
//
// Example:
//   node updater/artifacts.js v0.1.0 --module ./updater --module ./device-service
//
// The artifact is created in a temp directory, SHA256 is computed, and the file is
// moved to ./data/artifacts/<sha256>. Run from backend/ directory.

import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import { spawnSync } from 'child_process';

// Always emit into backend/data/artifacts so callers can run this script from any directory.
const ARTIFACTS_DIR = path.resolve(path.join(path.dirname(new URL(import.meta.url).pathname), '..', 'backend', 'data', 'artifacts'));
const MANIFEST_FILE = 'artifact.json';
const UPDATE_SCRIPT = 'update.sh';
const SETUP_SCRIPT = 'setup.sh';

function parseArgs(argv) {
  const out = { modules: [], version: null, script: null, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--help' || a === '-h') {
      out.help = true;
    } else if (a === '--module' || a === '-m') {
      const val = argv[++i];
      if (val) out.modules.push(val);
    } else if (a === '--script' || a === '-s') {
      out.script = argv[++i];
    } else if (!a.startsWith('-') && !out.version) {
      out.version = a;
    }
  }
  return out;
}

function usage(exitCode = 0) {
  const msg = `Usage:
  node updater/artifacts.js <version> --module <path> [--module <path>]... [--script <path>]

Arguments:
  <version>           Version tag (mandatory, e.g. v0.1.0)
  --module, -m <path> Module directory to include (repeatable, order preserved)
  --script, -s <path> Custom update.sh script (optional)

Behavior:
  - Copies each module's ${MANIFEST_FILE} entries into the artifact
  - Validates that each module has a setup.sh in its root (warns if missing)
  - Generates update.sh that calls each module's setup.sh in order (unless --script provided)
  - Creates VERSION file from <version> argument
  - Builds inner tar.gz payload
  - Computes SHA256 of inner tar.gz, writes it to hash file
  - Builds outer tar (uncompressed) containing hash + data (inner tar.gz)
  - Outputs to ./data/artifacts/
  - Prints the SHA256 + filename to stdout

Example:
  cd backend
  node ../updater/artifacts.js v0.1.0 --module ../updater --module ../device-service
`;
  process.stderr.write(msg);
  process.exit(exitCode);
}

function generateUpdateScript(moduleNames) {
  const setupRelPaths = moduleNames
    .map((mod) => (mod.target ? path.posix.join(mod.target, SETUP_SCRIPT) : SETUP_SCRIPT));
  const calls = setupRelPaths
    .map((rel) => `run_if_present "$APP_DIR/${rel}"`)
    .join('\n');
  const files = moduleNames.flatMap((mod) => mod.manifest.include
    .filter((item) => item !== SETUP_SCRIPT)
    .map((item) => {
      const rel = normalizeRelativePath(item);
      return mod.target ? path.posix.join(mod.target, rel) : rel;
    }));

  return `#!/bin/sh
set -eu

log() { printf '[update.sh] %s\\n' "$*" >&2; }

APP_DIR="$(pwd)"
STATE_PATH="$APP_DIR/state.json"

rm_if_present() {
  p="$1"
  if [ -f "$p" ] || [ -L "$p" ]; then
    rm -f "$p" || true
  fi
}

run_if_present() {
  p="$1"
  if [ ! -f "$p" ]; then
    return 0
  fi
  log "run: $p"
  if [ -x "$p" ]; then
    "$p"
  else
    sh "$p"
  fi
  rm_if_present "$p"
}

update_state_files() {
  python3 - "$STATE_PATH" "$APP_DIR" <<'PY'
import json
import os
import sys

state_path, install_root = sys.argv[1:]

try:
    with open(state_path, "r", encoding="utf-8") as state_file:
        state = json.load(state_file)
except FileNotFoundError:
    state = {}

new_files = ${JSON.stringify(files)}

old_files = state.get("files", [])
new_file_set = set(new_files)
for path in old_files:
    if path in new_file_set:
        continue
    full_path = os.path.join(install_root, path)
    if os.path.exists(full_path):
        os.remove(full_path)

state["files"] = new_files
tmp_path = f"{state_path}.tmp"
with open(tmp_path, "w", encoding="utf-8") as state_file:
    json.dump(state, state_file, indent=2)
    state_file.write("\\n")
os.replace(tmp_path, state_path)
PY
}

update_state_files

${calls}

if [ -n "\${VO_INSTALL_ROOT:-}" ]; then
  rm_if_present "$VO_INSTALL_ROOT/${UPDATE_SCRIPT}"
fi
rm_if_present "$0"

exit 0
`;
}

function normalizeRelativePath(relPath) {
  const normalized = String(relPath).replace(/\\/g, '/').trim();
  if (!normalized || normalized.startsWith('/') || normalized.split('/').some((part) => !part || part === '.' || part === '..')) {
    throw new Error(`unsafe manifest path: ${normalized}`);
  }
  return normalized;
}

function buildTarFromDir(dirPath, outTarPath, { gzip } = { gzip: true }) {
  const st = fs.statSync(dirPath);
  if (!st.isDirectory()) throw new Error(`input must be a directory: ${dirPath}`);
  const args = ['-C', dirPath];
  if (gzip) args.push('-czf'); else args.push('-cf');
  args.push(outTarPath, '.');
  const proc = spawnSync('tar', args, { stdio: 'pipe' });
  if (proc.error) throw proc.error;
  if (proc.status !== 0) {
    const stderr = (proc.stderr || Buffer.alloc(0)).toString('utf-8').trim();
    throw new Error(`tar failed (exit ${proc.status}): ${stderr || 'unknown error'}`);
  }
}

function sha256FileHex(filePath) {
  const buf = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(buf).digest('hex');
}

async function main() {
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);

  if (args.help) usage(0);
  if (!args.version) {
    process.stderr.write('error: version argument is required\n\n');
    usage(2);
  }
  if (args.modules.length === 0) {
    process.stderr.write('error: at least one --module is required\n\n');
    usage(2);
  }

  // Resolve module paths and validate
  const resolvedModules = [];
  for (const mod of args.modules) {
    const resolved = path.resolve(mod);
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
      throw new Error(`module not found or not a directory: ${mod}`);
    }
    const name = path.basename(resolved);
    const setupPath = path.join(resolved, SETUP_SCRIPT);
    if (!fs.existsSync(setupPath)) {
      process.stderr.write(`[artifacts] warn: ${name}/${SETUP_SCRIPT} not found\n`);
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(resolved, MANIFEST_FILE), 'utf-8'));
    const target = manifest.target ? normalizeRelativePath(manifest.target) : '';
    resolvedModules.push({ path: resolved, name, setupPath, manifest, target });
  }

  // Prepare staging directory
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vo-artifact-'));
  const stagingDir = path.join(tmpDir, 'staging');
  fs.mkdirSync(stagingDir, { recursive: true });

  try {
    // Copy each module
    for (const mod of resolvedModules) {
      for (const item of mod.manifest.include) {
        const rel = normalizeRelativePath(item);
        if (rel === SETUP_SCRIPT) continue;
        const dest = path.join(stagingDir, mod.target ? path.posix.join(mod.target, rel) : rel);
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.copyFileSync(path.join(mod.path, rel), dest);
      }
      const setupDest = mod.target ? path.posix.join(mod.target, SETUP_SCRIPT) : SETUP_SCRIPT;
      if (fs.existsSync(mod.setupPath)) {
        const dest = path.join(stagingDir, setupDest);
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.copyFileSync(mod.setupPath, dest);
      }
      process.stderr.write(`[artifacts] added module: ${mod.name}\n`);
    }

    // Generate or copy update.sh
    const updateScriptPath = path.join(stagingDir, UPDATE_SCRIPT);
    if (args.script) {
      const src = path.resolve(args.script);
      if (!fs.existsSync(src) || !fs.statSync(src).isFile()) {
        throw new Error(`--script not found: ${src}`);
      }
      fs.copyFileSync(src, updateScriptPath);
      fs.chmodSync(updateScriptPath, 0o755);
      process.stderr.write(`[artifacts] using custom ${UPDATE_SCRIPT} from ${src}\n`);
    } else {
      const moduleNames = resolvedModules.map((m) => m.name);
      const script = generateUpdateScript(resolvedModules);
      fs.writeFileSync(updateScriptPath, script, 'utf-8');
      fs.chmodSync(updateScriptPath, 0o755);
      process.stderr.write(`[artifacts] generated ${UPDATE_SCRIPT} for modules: ${moduleNames.join(', ')}\n`);
    }

    // Create VERSION file
    const versionPath = path.join(stagingDir, 'VERSION');
    fs.writeFileSync(versionPath, args.version + '\n', 'utf-8');
    process.stderr.write(`[artifacts] created VERSION: ${args.version}\n`);

    // Build inner payload tar.gz (without hash file)
    const innerTarPath = path.join(tmpDir, 'artifact.tar.gz');
    buildTarFromDir(stagingDir, innerTarPath, { gzip: true });

    // Compute SHA256 of inner payload
    const sha256 = sha256FileHex(innerTarPath);
    process.stderr.write(`[artifacts] payload sha256: ${sha256}\n`);

    // Build outer tar (uncompressed) with hash + inner tar.gz (named data)
    const outerDir = path.join(tmpDir, 'outer');
    fs.mkdirSync(outerDir, { recursive: true });
    const hashFilePath = path.join(outerDir, 'hash');
    fs.writeFileSync(hashFilePath, sha256 + '\n', 'utf-8');
    fs.copyFileSync(innerTarPath, path.join(outerDir, 'data'));
    const outerTarPath = path.join(tmpDir, 'artifact.tar');
    buildTarFromDir(outerDir, outerTarPath, { gzip: false });
    const sizeBytes = fs.statSync(outerTarPath).size;

    // Output to ./<ARTIFACTS_DIR>/<sha256>
    const artifactsDir = path.resolve(ARTIFACTS_DIR);
    fs.mkdirSync(artifactsDir, { recursive: true });
    const filename = sha256;
    const finalPath = path.join(artifactsDir, filename);

    if (fs.existsSync(finalPath)) {
      process.stderr.write(`[artifacts] artifact already exists: ${filename}\n`);
    } else {
      fs.copyFileSync(outerTarPath, finalPath);
      process.stderr.write(`[artifacts] created: ${ARTIFACTS_DIR}/${filename}\n`);
    }

    // Output result
    process.stdout.write(
      JSON.stringify(
        {
          ok: true,
          version: args.version,
          sha256,
          filename,
          sizeBytes,
          modules: resolvedModules.map((m) => m.name),
          path: `${ARTIFACTS_DIR}/${filename}`,
        },
        null,
        2
      ) + '\n'
    );
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  process.stderr.write(`error: ${err?.message || String(err)}\n`);
  process.exit(1);
});
