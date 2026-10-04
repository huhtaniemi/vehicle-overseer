import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import * as artifacts from '../src/artifacts_cli.js';

function parseResult(output) {
  const start = output.indexOf('{\n  "ok":');
  assert.ok(start >= 0, output);
  return JSON.parse(output.slice(start));
}

function storageFor(context) {
  const db = artifacts.openDb(context);
  const entryPoint = fileURLToPath(new URL('../src/index.js', import.meta.url));
  fs.mkdirSync(path.join(context.rootDir, 'data/artifacts'), { recursive: true });
  const runCli = (argv) => {
    const result = spawnSync(process.execPath, [entryPoint, 'artifacts', ...argv], {
      cwd: context.rootDir, encoding: 'utf-8'
    });
    assert.equal(result.status, 0, result.stderr);
    return {
      ...parseResult(result.stdout),
      warnings: result.stderr.split('\n').filter(line => line.startsWith('[refresh] warn: '))
        .map(line => line.slice('[refresh] warn: '.length))
    };
  };
  return {
    ...db,
    getDeviceTarget: (uid) => {
      const record = db.getDeviceTarget(uid);
      return { desiredVersion: record?.desired_version ?? null, updated_at: record?.updated_at ?? null };
    },
    resolveVersion: (version = 'latest') => {
      if (!version || version === 'latest') version = db.getLatestVersion()?.version;
      if (!version) return null;
      const record = db.getVersion(version);
      if (!record) return null;
      const artifact = db.getArtifact(record.artifact_id);
      return artifact ? { ...artifact, version } : null;
    },
    importArtifact: (filePath, options = {}) => runCli(['import', filePath, ...(options.force ? ['--force'] : [])]),
    refreshArtifacts: () => runCli(['refresh']),
    openArtifact: (id) => {
      const artifact = db.getArtifact(id);
      return artifact ? fs.createReadStream(path.join(context.rootDir, 'data/artifacts', artifact.filename)) : null;
    }
  };
}

function fixture(context) {
  const rootDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vo-storage-'));
  context.after(() => fs.rmSync(rootDir, { recursive: true, force: true }));
  return { rootDir, storage: storageFor({ rootDir }) };
}

function makePackage(rootDir, version, date, content = version) {
  const work = fs.mkdtempSync(path.join(rootDir, 'package-'));
  fs.writeFileSync(path.join(work, 'VERSION'), version);
  fs.writeFileSync(path.join(work, 'payload'), content);
  const timestamp = new Date(date);
  fs.utimesSync(path.join(work, 'VERSION'), timestamp, timestamp);
  const pack = (args) => {
    const result = spawnSync('tar', args, { encoding: 'utf-8' });
    assert.equal(result.status, 0, result.stderr);
  };
  pack(['-C', work, '-czf', path.join(work, 'data'), './VERSION', './payload']);
  const id = crypto.createHash('sha256').update(fs.readFileSync(path.join(work, 'data'))).digest('hex');
  fs.writeFileSync(path.join(work, 'hash'), id);
  const filePath = path.join(rootDir, id);
  pack(['-C', work, '-cf', filePath, './hash', './data']);
  return { id, filePath };
}

test('target relations are readable symlinks with safe encoded keys', (context) => {
  const { rootDir, storage } = fixture(context);
  assert.equal(storage.getDeviceTarget('device-a').desiredVersion, null);
  storage.setDeviceTarget('device-a', 'v1.2.3');
  const target = path.join(rootDir, 'data/db/device_targets/device-a');
  assert.equal(fs.readlinkSync(target), '../versions/v1.2.3');
  assert.equal(storage.getDeviceTarget('device-a').updated_at, fs.lstatSync(target).mtime.toISOString());
  storage.setDeviceTarget('../outside', '../version');
  assert.equal(storage.getDeviceTarget('../outside').desiredVersion, '../version');
  storage.setDeviceTarget('device-a', null);
  assert.equal(storage.getDeviceTarget('device-a').desiredVersion, null);
});

test('key and token records persist with private permissions', (context) => {
  const { rootDir, storage } = fixture(context);
  const token = crypto.randomBytes(24).toString('base64url');
  const uid = crypto.randomBytes(16).toString('hex');
  const now = new Date().toISOString();
  const key = {
    key_id: crypto.randomBytes(8).toString('hex'), key_b64: crypto.randomBytes(32).toString('base64'),
    created_at: now, updated_at: now
  };
  assert.equal(storage.getBootstrapToken('unknown'), null);
  storage.begin();
  storage.insertBootstrapToken({ token, kind: 'one-time', created_at: now, used_at: null });
  storage.insertDeviceKey({ device_uid: uid, ...key });
  storage.markBootstrapTokenUsed(token, now);
  storage.commit();
  assert.ok(storage.getBootstrapToken(token).used_at);
  const reopened = storageFor({ rootDir });
  assert.deepEqual(reopened.getDeviceKey(uid), key);
  assert.equal(reopened.getBootstrapToken(token).used_at, now);
  assert.equal(fs.statSync(path.join(rootDir, 'data/db/device_keys', uid)).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(rootDir, 'data/db/bootstrap_tokens', token)).mode & 0o777, 0o600);
  storage.begin();
  storage.insertDeviceKey({ device_uid: uid, ...key, key_id: 'not-committed' });
  storage.rollback();
  assert.deepEqual(reopened.getDeviceKey(uid), key);
});

test('artifacts, versions, latest, and device targets resolve through symlinks', async (context) => {
  const { rootDir, storage } = fixture(context);
  const older = makePackage(rootDir, 'v9.0.0', '2025-01-01T00:00:00Z');
  const newer = makePackage(rootDir, 'v1.0.0', '2026-01-01T00:00:00Z');
  storage.importArtifact(older.filePath);
  storage.importArtifact(newer.filePath);
  assert.equal(storage.resolveVersion().id, newer.id);
  assert.equal(storage.resolveVersion().version, 'v1.0.0');
  assert.equal(storage.resolveVersion('missing'), null);
  assert.equal(fs.readlinkSync(path.join(rootDir, 'data/db/artifacts', newer.id)), `../../artifacts/${newer.id}`);
  assert.equal(fs.readlinkSync(path.join(rootDir, 'data/db/versions/v1.0.0')), `../artifacts/${newer.id}`);
  const linkStat = fs.lstatSync(path.join(rootDir, 'data/db/artifacts', newer.id));
  assert.equal(storage.getArtifact(newer.id).inserted_at, linkStat.mtime.toISOString());
  storage.setDeviceTarget('device-a', 'v9.0.0');
  assert.equal(storage.resolveVersion(storage.getDeviceTarget('device-a').desiredVersion).id, older.id);
  storage.setDeviceTarget('device-a', 'latest');
  assert.equal(storage.resolveVersion(storage.getDeviceTarget('device-a').desiredVersion).id, newer.id);
  const chunks = [];
  for await (const chunk of storage.openArtifact(newer.id)) chunks.push(chunk);
  assert.deepEqual(Buffer.concat(chunks), fs.readFileSync(newer.filePath));
  const reopened = storageFor({ rootDir });
  assert.equal(reopened.resolveVersion().id, newer.id);
});

test('imports refuse conflicting versions unless forced and use version text to break date ties', (context) => {
  const { rootDir, storage } = fixture(context);
  const first = makePackage(rootDir, 'v1.0.0', '2026-01-01T00:00:00Z', 'first');
  const replacement = makePackage(rootDir, 'v1.0.0', '2026-01-02T00:00:00Z', 'replacement');
  storage.importArtifact(first.filePath);
  assert.equal(storage.importArtifact(replacement.filePath).conflict, true);
  assert.equal(storage.resolveVersion('v1.0.0').id, first.id);
  storage.importArtifact(replacement.filePath, { force: true });
  assert.equal(storage.resolveVersion('v1.0.0').id, replacement.id);
  const tied = makePackage(rootDir, 'v2.0.0', '2026-01-02T00:00:00Z');
  storage.importArtifact(tied.filePath);
  assert.equal(storage.resolveVersion().id, tied.id);
});

test('refresh adds packages, preserves insertion time, warns on conflicts, and removes missing packages', (context) => {
  const { rootDir, storage } = fixture(context);
  const first = makePackage(rootDir, 'v1.0.0', '2026-01-01T00:00:00Z');
  const conflict = makePackage(rootDir, 'v1.0.0', '2026-01-02T00:00:00Z');
  const destination = path.join(rootDir, 'data/artifacts', first.id);
  fs.copyFileSync(first.filePath, destination);
  assert.equal(storage.refreshArtifacts().added, 1);
  const insertedAt = storage.getArtifact(first.id).inserted_at;
  assert.equal(storage.refreshArtifacts().added, 0);
  assert.equal(storage.getArtifact(first.id).inserted_at, insertedAt);
  fs.copyFileSync(conflict.filePath, path.join(rootDir, 'data/artifacts', conflict.id));
  assert.ok(storage.refreshArtifacts().warnings.some(warning => warning.startsWith('conflict version')));
  assert.equal(storage.resolveVersion('v1.0.0').id, first.id);
  fs.rmSync(path.join(rootDir, 'data/artifacts', conflict.id));
  fs.rmSync(destination);
  assert.equal(storage.refreshArtifacts().removed, 1);
  assert.equal(storage.getArtifact(first.id), null);
  assert.equal(storage.resolveVersion(), null);
  assert.deepEqual(storage.listVersions(), []);
});

test('writers fail explicitly when another process holds the lock', (context) => {
  const { rootDir, storage } = fixture(context);
  const lock = path.join(rootDir, 'data/db/.writer-lock');
  fs.mkdirSync(lock);
  assert.throws(() => storage.begin(), /database busy/);
  fs.rmdirSync(lock);
  storage.begin();
  storage.rollback();
});

test('HTTP provisioning, manifests, encrypted downloads, external CLI updates, and restarts remain compatible', { timeout: 30000 }, async (context) => {
  const { rootDir, storage } = fixture(context);
  const listener = createServer();
  listener.listen(0, '127.0.0.1');
  await once(listener, 'listening');
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  fs.writeFileSync(path.join(rootDir, 'config.json'), JSON.stringify({ httpHost: '127.0.0.1', httpPort: port }));
  const entryPoint = fileURLToPath(new URL('../src/index.js', import.meta.url));
  let server;
  const stopServer = async () => {
    if (!server || server.exitCode !== null || server.signalCode !== null) return;
    const exited = once(server, 'exit');
    server.kill();
    await exited;
  };
  const startServer = async () => {
    server = spawn(process.execPath, [entryPoint], { cwd: rootDir, stdio: ['ignore', 'pipe', 'pipe'] });
    await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(new Error(`server startup timeout: ${output}`)), 10000);
      const failed = () => {
        clearTimeout(timer);
        reject(new Error(`server exited during startup: ${output}`));
      };
      server.once('exit', failed);
      server.once('error', (error) => {
        clearTimeout(timer);
        reject(error);
      });
      server.stderr.on('data', (chunk) => { output += chunk; });
      server.stdout.on('data', (chunk) => {
        output += chunk;
        if (output.includes('Backend listening')) {
          clearTimeout(timer);
          server.off('exit', failed);
          resolve();
        }
      });
    });
  };
  const request = (pathname, body) => fetch(`http://127.0.0.1:${port}${pathname}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  try {
    await startServer();
    const tokenResponse = await request('/api/bootstrap-token', { kind: 'one-time' });
    assert.equal(tokenResponse.status, 200);
    const { token } = await tokenResponse.json();
    const keyResponse = await request(`/api/device/key?token=${token}`);
    assert.equal(keyResponse.status, 200);
    const [uid, keyB64] = (await keyResponse.text()).trim().split('\n');
    assert.equal((await request(`/api/device/key?token=${token}`)).status, 403);
    const manifestUrl = `/api/device/manifest?uid=${uid}`;
    assert.equal((await (await request(manifestUrl)).json()).artifact, null);
    const first = makePackage(rootDir, 'v1.0.0', '2026-01-01T00:00:00Z');
    const second = makePackage(rootDir, 'v2.0.0', '2026-01-02T00:00:00Z');
    for (const artifact of [first, second]) {
      const imported = spawnSync(process.execPath, [entryPoint, 'artifacts', 'import', artifact.filePath], {
        cwd: rootDir, encoding: 'utf-8'
      });
      assert.equal(imported.status, 0, imported.stderr);
      assert.equal(parseResult(imported.stdout).ok, true);
    }
    const manifest = await (await request(manifestUrl)).json();
    assert.equal(manifest.version, 'v2.0.0');
    assert.equal(manifest.artifact.id, second.id);
    const downloadUrl = `${manifest.artifact.url}?uid=${uid}`;
    const download = await request(downloadUrl);
    assert.equal(download.status, 200);
    assert.equal(download.headers.get('X-VO-Enc'), 'aes-256-ctr');
    assert.equal(download.headers.get('X-VO-KeyId'), storage.getDeviceKey(uid).key_id);
    const decipher = crypto.createDecipheriv('aes-256-ctr', Buffer.from(keyB64, 'base64'), Buffer.from(download.headers.get('X-VO-Iv'), 'hex'));
    const encrypted = Buffer.from(await download.arrayBuffer());
    assert.deepEqual(Buffer.concat([decipher.update(encrypted), decipher.final()]), fs.readFileSync(second.filePath));
    assert.equal((await request(`${manifest.artifact.url}?uid=unknown`)).status, 403);
    storage.setDeviceTarget(uid, 'v1.0.0');
    assert.equal((await (await request(manifestUrl)).json()).artifact.id, first.id);
    storage.setDeviceTarget(uid, 'missing');
    assert.equal((await (await request(manifestUrl)).json()).artifact, null);
    storage.setDeviceTarget(uid, 'latest');
    await request('/api/ping', { uid, label: 'test-device', 'ip-address': '127.0.0.1', state: 'ready', data: { version: 'v1.0.0' } });
    const entries = await (await request('/api/entries')).json();
    assert.equal(entries.entries[0].state, 'ready');
    await stopServer();
    await startServer();
    assert.deepEqual((await (await request('/api/entries')).json()).entries, []);
    assert.equal((await request(`/api/device/key?token=${token}`)).status, 403);
    assert.equal((await (await request(manifestUrl)).json()).artifact.id, second.id);
    const restartedDownload = await request(downloadUrl);
    assert.equal(restartedDownload.status, 200);
    await restartedDownload.arrayBuffer();
  } finally {
    await stopServer();
  }
});