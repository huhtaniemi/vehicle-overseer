// Backend-side artifact CLI.

import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';

const ARTIFACTS_DIR = 'data/artifacts';

// Usage:
//   node backend/src/index.js artifacts import /path/to/artifact-file
//   node backend/src/index.js artifacts refresh
//   ./vehicle-overseer-backend artifacts import /path/to/artifact-file
//   ./vehicle-overseer-backend artifacts refresh

function usage(exitCode = 0) {
  const msg = `Usage:
  artifacts import <file> [--force]   import artifact and sync to database (force overwrites version mapping)
  artifacts refresh         scan ${ARTIFACTS_DIR}/ and sync to database

ID is read from hash file inside the tarball.
Version is read from VERSION file inside the tarball.
Missing artifacts are removed from database on refresh.
`;
  process.stderr.write(msg);
  process.exit(exitCode);
}

function readFileFromTar(filePath, member, { encoding = 'utf-8' } = {}) {
  const proc = spawnSync('tar', ['-xOf', filePath, member], { encoding, stdio: ['pipe', 'pipe', 'pipe'] });
  if (proc.error || proc.status !== 0) return null;
  return proc.stdout;
}

function readFileFromTarGzBytes(bytes, candidates) {
  for (const candidate of candidates) {
    const proc = spawnSync('tar', ['-xOzf', '-', candidate], {
      encoding: 'utf-8',
      input: bytes,
      stdio: ['pipe', 'pipe', 'pipe']
    });
    if (proc.error) continue;
    if (proc.status === 0) {
      const value = String(proc.stdout || '').trim();
      if (value) return value;
    }
  }
  return null;
}

function readFileDateFromTarGzBytes(bytes, candidates) {
  const proc = spawnSync('tar', ['--full-time', '-tzvf', '-'], {
    encoding: 'utf-8',
    input: bytes,
    stdio: ['pipe', 'pipe', 'pipe']
  });
  let date = null;
  if (proc.status === 0 && proc.stdout) {
    const lines = String(proc.stdout || '').split('\n');
    for (const candidate of candidates) {
      for (const line of lines) {
        if (line.includes(candidate)) {
          const match = line.match(/(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
          if (match) {
            const parsed = new Date(match[1]);
            if (!Number.isNaN(parsed.getTime())) date = parsed.toISOString();
          }
          break;
        }
      }
    }
  }
  return date;
}

function readFileFromTarGz(filePath, candidates) {
  for (const candidate of candidates) {
    const proc = spawnSync('tar', ['-xOzf', filePath, candidate], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] });
    if (proc.error) continue;
    if (proc.status === 0) {
      const value = String(proc.stdout || '').trim();
      if (value) return value;
    }
  }
  return null;
}

function readIdFromArtifact(filePath) {
  // Outer tar must hold hash + data (inner tar.gz)
  const hashvalue = readFileFromTar(filePath, './hash');
  return hashvalue ? String(hashvalue).trim() : null;
}

function readVersionAndDateFromArtifact(filePath) {
  // Read VERSION content and its timestamp from inner tar.gz; no filesystem fallback
  const dat = readFileFromTar(filePath, './data', { encoding: 'buffer' });
  return dat ? {
    version: readFileFromTarGzBytes(dat, ['./VERSION']),
    date: readFileDateFromTarGzBytes(dat, ['./VERSION'])
  } : { version: null, date: null };
}

export function openDb({ rootDir, config = {} }) {
  const dbPath = path.resolve(rootDir, config.dbPath || './data/db');
  const artifactsDir = path.resolve(rootDir, ARTIFACTS_DIR);
  const tables = ['artifacts', 'versions', 'device_targets', 'device_keys', 'bootstrap_tokens'];
  for (const table of tables) fs.mkdirSync(path.join(dbPath, table), { recursive: true, mode: 0o700 });
  const lockPath = path.join(dbPath, '.writer-lock');
  let pending = null;

  const recordPath = (table, key) => path.join(dbPath, table, encodeURIComponent(key).replace(/^\./, '%2E'));
  const readLink = (table, key) => {
    const filename = recordPath(table, key);
    if (pending?.has(filename)) return pending.get(filename)?.value ?? null;
    try {
      return fs.readlinkSync(filename);
    } catch (err) {
      if (err.code === 'ENOENT') return null;
      throw err;
    }
  };
  const readRecord = (table, key) => {
    const filename = recordPath(table, key);
    if (pending?.has(filename)) return pending.get(filename)?.value ?? null;
    try {
      return JSON.parse(fs.readFileSync(filename, 'utf-8'));
    } catch (err) {
      if (err.code === 'ENOENT') return null;
      throw err;
    }
  };
  const listKeys = (table) => {
    const directory = path.join(dbPath, table);
    const keys = new Set(fs.readdirSync(directory).filter(name => !name.startsWith('.')));
    if (pending) {
      for (const [filename, record] of pending) {
        if (path.dirname(filename) !== directory) continue;
        if (record) keys.add(path.basename(filename));
        else keys.delete(path.basename(filename));
      }
    }
    return [...keys].map(name => decodeURIComponent(name));
  };
  const writeAtomic = (filename, record) => {
    if (!record) return fs.rmSync(filename, { force: true });
    const tmp = path.join(path.dirname(filename), `.tmp-${process.pid}-${Date.now()}`);
    try {
      if (record.kind === 'link') fs.symlinkSync(record.value, tmp);
      else fs.writeFileSync(tmp, JSON.stringify(record.value, null, 2) + '\n', { mode: 0o600 });
      fs.renameSync(tmp, filename);
    } finally {
      fs.rmSync(tmp, { force: true });
    }
  };
  const write = (table, key, record) => {
    const filename = recordPath(table, key);
    if (pending) pending.set(filename, record);
    else writeAtomic(filename, record);
  };
  const writeLink = (table, key, destination) => write(table, key, {
    kind: 'link', value: path.relative(path.join(dbPath, table), destination)
  });
  const writeRecord = (table, key, record) => write(table, key, { kind: 'json', value: record });
  const getArtifact = (id) => {
    const link = readLink('artifacts', id);
    if (link === null) return null;
    const filePath = path.resolve(path.join(dbPath, 'artifacts'), link);
    try {
      const stat = fs.statSync(filePath);
      const { version, date } = readVersionAndDateFromArtifact(filePath);
      const filename = recordPath('artifacts', id);
      return {
        id, filename: path.basename(filePath), size_bytes: stat.size, created_at: date, version,
        inserted_at: pending?.has(filename) ? null : fs.lstatSync(filename).mtime.toISOString()
      };
    } catch (err) {
      if (err.code === 'ENOENT') return null;
      throw err;
    }
  };
  const getVersion = (version) => {
    const link = readLink('versions', version);
    return link === null ? null : { version, artifact_id: decodeURIComponent(path.basename(link)) };
  };
  const listVersions = () => listKeys('versions').map(version => {
    const record = getVersion(version);
    const artifact = getArtifact(record.artifact_id);
    return { ...record, size_bytes: artifact?.size_bytes ?? null, created_at: artifact?.created_at ?? null };
  });
  const getLatestVersion = () => {
    const versions = listVersions().filter(record => record.version !== 'latest' && getArtifact(record.artifact_id));
    versions.sort((first, second) => {
      const dateOrder = (Date.parse(second.created_at) || 0) - (Date.parse(first.created_at) || 0);
      return dateOrder || (first.version < second.version ? 1 : first.version > second.version ? -1 : 0);
    });
    return versions.length ? { version: versions[0].version, artifact_id: versions[0].artifact_id } : null;
  };
  const rollback = () => {
    if (!pending) return;
    pending = null;
    fs.rmdirSync(lockPath);
  };

  return {
    getArtifact,
    getVersion,
    listVersions,
    getLatestVersion,
    listArtifacts: () => listKeys('artifacts').map(id => ({ id })),
    insertArtifact: ({ id, filename }, replace = false) => {
      if (replace || readLink('artifacts', id) === null) writeLink('artifacts', id, path.join(artifactsDir, filename));
    },
    insertVersion: (version, id, replace = false) => {
      if (replace || getVersion(version) === null) writeLink('versions', version, recordPath('artifacts', id));
    },
    deleteArtifact: (id) => write('artifacts', id, null),
    deleteVersionsForArtifact: (id) => {
      for (const version of listKeys('versions')) {
        if (getVersion(version).artifact_id === id) write('versions', version, null);
      }
    },
    getDeviceTarget: (uid) => {
      const link = readLink('device_targets', uid);
      if (link === null) return null;
      return {
        desired_version: decodeURIComponent(path.basename(link)),
        updated_at: fs.lstatSync(recordPath('device_targets', uid)).mtime.toISOString()
      };
    },
    setDeviceTarget: (uid, version) => {
      if (!version) write('device_targets', uid, null);
      else writeLink('device_targets', uid, recordPath('versions', version));
    },
    getDeviceKey: (uid) => readRecord('device_keys', uid),
    insertDeviceKey: ({ device_uid, ...record }) => writeRecord('device_keys', device_uid, record),
    getBootstrapToken: (token) => readRecord('bootstrap_tokens', token),
    insertBootstrapToken: ({ token, ...record }) => writeRecord('bootstrap_tokens', token, record),
    markBootstrapTokenUsed: (token, usedAt) => {
      const record = readRecord('bootstrap_tokens', token);
      if (record && !record.used_at) writeRecord('bootstrap_tokens', token, { ...record, used_at: usedAt });
    },
    begin: () => {
      try {
        fs.mkdirSync(lockPath, { mode: 0o700 });
      } catch (err) {
        if (err.code === 'EEXIST') throw new Error('database busy: another writer holds .writer-lock');
        throw err;
      }
      pending = new Map();
    },
    commit: () => {
      if (!pending) return;
      try {
        for (const [filename, record] of pending) writeAtomic(filename, record);
      } finally {
        rollback();
      }
    },
    rollback
  };
}

function updateArtifact(run, { id, filename, sizeBytes, createdAt, version }) {
  run.insertArtifact({ id, filename, sizeBytes, createdAt });
  run.insertVersion(version, id);
}

function upsertArtifact(run, { id, filename, sizeBytes, createdAt, version }) {
  run.insertArtifact({ id, filename, sizeBytes, createdAt }, true);
  run.insertVersion(version, id, true);
}

function upsertArtifactAndVersion(run, mode, id, version, filename, sizeBytes, createdAt, options = {}) {
  const { force = false } = options;
  let conflictInfo = null;
  let versionInserted = false;

  switch (mode) {
    case 'refresh':
      updateArtifact(run, { id, filename, sizeBytes, createdAt, version });
      versionInserted = true;
      break;

    case 'import':
      const existingVersion = run.getVersion(version);
      if (existingVersion && String(existingVersion.artifact_id) !== String(id)) {
        const existingArtifact = run.getArtifact(existingVersion.artifact_id);
        conflictInfo = existingArtifact
          ? {
              existingId: String(existingArtifact.id),
              existingSize: Number(existingArtifact.size_bytes),
              existingCreatedAt: String(existingArtifact.created_at)
            }
          : { existingId: String(existingVersion.artifact_id), existingSize: null, existingCreatedAt: null };
        if (!force) {
          return null;
        }
      }

      upsertArtifact(run, { id, filename, sizeBytes, createdAt, version });

      versionInserted = true;
      break;

    default:
      throw new Error(`unknown mode for upsertArtifactAndVersion: ${mode}`);
  }

  return { versionInserted, conflictInfo };
}

function updateLatest(run) {
  // Maintain synthetic 'latest' using artifact created_at, falling back to version desc.
  const newestVersion = run.getLatestVersion();
  const rows = newestVersion ? [newestVersion] : undefined;
  process.stdout.write('rows -> ' + JSON.stringify(rows) + '\n');
  const newest = rows?.length ? rows[0] : {};
  if (!newest?.version || !newest?.artifact_id) return;
  run.insertVersion('latest', newest.artifact_id, true);
}


async function cmdImport({ rootDir, config, filePath, force }) {
  const resolvedFile = path.resolve(filePath);
  if (!fs.existsSync(resolvedFile) || !fs.statSync(resolvedFile).isFile()) {
    throw new Error(`file not found: ${resolvedFile}`);
  }

  // Read id from hash file inside tarball
  const id = readIdFromArtifact(resolvedFile);
  if (!id) {
    throw new Error('hash file not found in artifact');
  }

  // Read version + date from tarball
  const { version, date } = readVersionAndDateFromArtifact(resolvedFile);
  if (!version || !date) {
    throw new Error('data or VERSION file not found in artifact or missing timestamp');
  }

  const artifactsDir = path.resolve(rootDir, ARTIFACTS_DIR);
  fs.mkdirSync(artifactsDir, { recursive: true });

  const filename = path.basename(resolvedFile);
  const destPath = path.join(artifactsDir, filename);
 
  if (fs.existsSync(destPath)) {
    process.stderr.write(`[import] artifact already exists: ${id}\n`);
  } else {
    fs.copyFileSync(resolvedFile, destPath);
    process.stderr.write(`[import] copied to ${ARTIFACTS_DIR}/${filename}\n`);
  }

  const sizeBytes = fs.statSync(destPath).size;

  // Update database
  const db = openDb({ rootDir, config });
  const run = db;
  // process.stdout.write('run('+operation+') -> ' + JSON.stringify(params) + '\n');

  run.begin();
  let conflictInfo = null;
  let versionInserted = false;
  let upsertResult = upsertArtifactAndVersion(run, 'import', id, version, filename, sizeBytes, date, { force });
  if (upsertResult === null) {
    // Conflict, not forced
    try { run.rollback(); } catch { /* ignore */ }
    const msg = `version ${version} already mapped to artifact (use --force to overwrite)`;
    process.stderr.write(msg + '\n');
    process.stdout.write(
      JSON.stringify({ ok: false, mode: 'import', id, filename, version, sizeBytes, conflict: true, message: msg }, null, 2) + '\n'
    );
    return 0;
  } else {
    versionInserted = upsertResult.versionInserted;
    conflictInfo = upsertResult.conflictInfo;
  }

  updateLatest(run);
  run.commit();

  process.stdout.write(
    JSON.stringify({ ok: true, mode: 'import', id, filename, version, sizeBytes }, null, 2) + '\n'
  );

  if (conflictInfo) {
    process.stderr.write(
      `[import] version ${version} overwrite - disk: ${id} (size ${sizeBytes} bytes, date ${date}), db: ${conflictInfo.existingId} (size ${conflictInfo.existingSize ?? 'unknown'} bytes, date ${conflictInfo.existingCreatedAt ?? 'unknown'})\n`
    );
  }

  return 0;
}

async function cmdRefresh({ rootDir, config }) {
  const db = openDb({ rootDir, config });
  const run = db;
  // process.stdout.write('run('+operation+') -> ' + JSON.stringify(params) + '\n');

  // Scan disk - read id from hash file inside each artifact
  const artifactsDir = path.resolve(rootDir, ARTIFACTS_DIR);
  const diskArtifacts = new Map();
  if (!fs.existsSync(artifactsDir)) {
    process.stderr.write(`[refresh] artifacts dir missing; treating as empty: ${artifactsDir}\n`);
  }
  const entriesOnDisk = fs.existsSync(artifactsDir) ? fs.readdirSync(artifactsDir) : [];
  for (const filename of entriesOnDisk) {
    const filePath = path.join(artifactsDir, filename);
    const st = fs.statSync(filePath);
    if (!st.isFile()) continue;
    const id = readIdFromArtifact(filePath);
    if (!id) {
      process.stderr.write(`[refresh] warn: no hash file in ${filename}?!, skipping\n`);
      continue;
    }
    if (diskArtifacts.has(id)) {
      process.stderr.write(`[refresh] warn: duplicate artifact ID ${id}; skipping ${filename}\n`);
      continue;
    }
    const { version, date } = readVersionAndDateFromArtifact(filePath);
    // process.stderr.write(`${id} -> ${version} ${date}\n`);

    if (!version || !date) {
      process.stderr.write(`[refresh] warn: no VERSION found in ${id}\n`);
    }
    diskArtifacts.set(id, { path: filePath, filename, sizeBytes: st.size, version, createdAt: date, status: 'new' });
  }

  // get list of artifacts from DB
  const dbArtifacts = run.listArtifacts() || [];
  const dbIds = new Set(dbArtifacts.map((row) => String(row.id)));

  // process.stdout.write('dbArtifacts -> ' + JSON.stringify(dbArtifacts) + '\n');

  let added = 0;
  let removed = 0;

  run.begin();
  try {
    // Remove DB entries for missing artifacts (by id)
    for (const row of dbArtifacts) {
      const id = String(row.id);
      if (!diskArtifacts.has(id)) {
        run.deleteVersionsForArtifact(id);
        run.deleteArtifact(id);
        process.stderr.write(`[refresh] removed missing artifact from DB: ${id}\n`);
        removed++;
      }
    }

    const versionRows = run.listVersions().filter(row => row.version != 'latest');
    process.stdout.write('versionRows -> ' + JSON.stringify(versionRows, null, 2) + '\n');

    const existingVersions = new Map(versionRows.map(row => [row.version, {
      artifactId: row.artifact_id,
      sizeBytes: row.size_bytes,
      createdAt: row.created_at
    }]));

    for (const [id, info] of diskArtifacts.entries()) {
      if (!info.version || !info.createdAt) {
        info.status = 'no_version';
        continue;
      }
      const isNewArtifact = !dbIds.has(id);
      const existingEntry = existingVersions.get(info.version);
      if (isNewArtifact && existingEntry) {
        process.stderr.write(
          `[refresh] warn: conflict version ${info.version}  - disk: ${id} (size ${info.sizeBytes} bytes, date ${info.createdAt}), db: ${existingEntry.artifactId} (size ${existingEntry.sizeBytes} bytes, date ${existingEntry.createdAt})\n`
        );
        info.status = 'version_conflict';
        continue;
      }

      if (isNewArtifact) {
        process.stderr.write(`[refresh] added artifact: ${id}\n`);
      }

      if (isNewArtifact) {
        const { versionInserted } = upsertArtifactAndVersion(
          run, 'refresh', id, info.version, info.filename, info.sizeBytes, info.createdAt
        );
        if (versionInserted) {
          existingVersions?.set(info.version, {
            artifactId: id, sizeBytes: info.sizeBytes, createdAt: info.createdAt,
          });

          added++;
          info.status = 'inserted';
          process.stderr.write(`[refresh] added artifact ${id} -> version${info.version}\n`);
        }
      } else {
        info.status = 'present';
      }
    }

    updateLatest(run);

    run.commit();
  } catch (err) {
    try { run.rollback(); } catch { /* ignore */ }
    throw err;
  }

  // Build artifacts output with status from diskArtifacts + skipped items
  const artifacts = [];
  for (const [id, info] of diskArtifacts.entries()) {
    const result = {
      id,
      filename: info.filename,
      status: info.status,
      version: info.version,
      sizeBytes: info.sizeBytes
    };
    artifacts.push(result);
  }

  process.stdout.write(
    JSON.stringify({ ok: true, mode: 'refresh', added, removed, artifacts }, null, 2) + '\n'
  );

  return 0;
}

export async function runArtifactsCli({ argv, rootDir, config }) {
  const cmd = argv[0];

  if (cmd === '--help' || cmd === '-h' || !cmd) {
    usage(0);
  }

  if (cmd === 'import') {
    const filePath = argv[1];
    const force = argv.includes('--force') || argv.includes('-f');
    if (!filePath) {
      process.stderr.write('error: import requires a file path\n\n');
      usage(2);
    }
    return cmdImport({ rootDir, config, filePath, force });
  }

  if (cmd === 'refresh') {
    return cmdRefresh({ rootDir, config });
  }

  process.stderr.write(`error: unknown command '${cmd}'\n\n`);
  usage(2);
}

// Shared programmatic refresh (disk -> database) for reuse in index.js
export async function refreshArtifacts({ rootDir, config }) {
  return cmdRefresh({ rootDir, config });
}

