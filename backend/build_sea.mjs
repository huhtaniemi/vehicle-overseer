#!/usr/bin/env node

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const backendRoot = path.resolve(__dirname);
const repoRoot = path.resolve(backendRoot, '..');
const distDir = path.join(backendRoot, 'dist');

const binName = 'vehicle-overseer-backend';
const platform = process.platform;
const arch = process.arch;
const exeSuffix = platform === 'win32' ? '.exe' : '';

const SEA_FUSE = 'NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2';
const shouldTar = process.argv.includes('--tar');

const nodeMajor = Number(String(process.versions.node || '').split('.')[0] || '0');
if (nodeMajor < 20) {
  console.error(`[sea] Node >= 20 required to build SEA (current: ${process.versions.node}).`);
  console.error('[sea] Install Node 20+ on the build machine, then run: npm run build:sea');
  process.exit(2);
}

const tool = (name) => {
  const bin = platform === 'win32' ? `${name}.cmd` : name;
  return path.join(backendRoot, 'node_modules', '.bin', bin);
};

const run = (cmd, args, opts = {}) => {
  const proc = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (proc.error) throw proc.error;
  if (proc.status !== 0) throw new Error(`${cmd} failed with exit code ${proc.status}`);
};

const copyFile = (src, dst) => {
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
};

const copyDir = (srcDir, dstDir) => {
  fs.mkdirSync(dstDir, { recursive: true });
  for (const ent of fs.readdirSync(srcDir, { withFileTypes: true })) {
    const src = path.join(srcDir, ent.name);
    const dst = path.join(dstDir, ent.name);
    if (ent.isDirectory()) copyDir(src, dst);
    else if (ent.isFile()) copyFile(src, dst);
  }
};

const writeJson = (p, obj) => {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2) + '\n');
};

fs.mkdirSync(distDir, { recursive: true });

// 1) Bundle into a single file (SEA wants a single entry point)
const bundleOut = path.join(distDir, 'app.cjs');
run(tool('esbuild'), [
  path.join(backendRoot, 'src', 'index.js'),
  '--bundle',
  '--platform=node',
  '--target=node20',
  '--format=cjs',
  `--outfile=${bundleOut}`
]);

// 2) Build SEA blob (Node 20+)
const blobOut = path.join(distDir, 'sea-prep.blob');
const seaConfigPath = path.join(distDir, 'sea-config.json');
writeJson(seaConfigPath, {
  main: './app.cjs',
  output: './sea-prep.blob',
  disableExperimentalSEAWarning: true
});

// Node has used different flags across versions; try both.
const node = process.execPath;
try {
  run(node, ['--experimental-sea-config', seaConfigPath], { cwd: distDir });
} catch {
  run(node, ['--sea-config', seaConfigPath], { cwd: distDir });
}

// 3) Copy current node executable and inject the blob using postject
const outExe = path.join(distDir, `${binName}${exeSuffix}`);
copyFile(process.execPath, outExe);

const postjectArgs = [
  outExe,
  'NODE_SEA_BLOB',
  blobOut,
  '--sentinel-fuse',
  SEA_FUSE
];
if (platform === 'darwin') {
  postjectArgs.push('--macho-segment-name', 'NODE_SEA');
}
run(tool('postject'), postjectArgs, { cwd: backendRoot });

// 4) Copy runtime assets next to the binary
// Service/provisioning assets: backend serves these at runtime.
copyFile(path.join(repoRoot, 'updater', 'srvcsetup.sh'), path.join(distDir, 'updater', 'srvcsetup.sh'));
copyFile(
  path.join(repoRoot, 'updater', 'updater.py'),
  path.join(distDir, 'updater', 'updater.py')
);
copyDir(path.join(repoRoot, 'updater', 'systemd'), path.join(distDir, 'updater', 'systemd'));

// Optional: package the entire dist/ directory for easy server transport.
if (shouldTar) {
  const tarName = `${binName}-${platform}-${arch}.tar.gz`;
  const tarPath = path.join(distDir, tarName);
  run('tar', ['-C', distDir, '-czf', tarPath, '.']);
  console.log(`Packaged dist tarball: ${tarPath}`);
}

console.log(`\nBuilt SEA executable: ${outExe}`);
console.log(`Dist dir: ${distDir}`);
console.log('Deploy by copying dist/ to the target machine; run the executable from inside that folder (cwd controls runtime paths).');
