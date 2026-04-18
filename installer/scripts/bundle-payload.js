#!/usr/bin/env node
// Pre-publish step: copy repo files that the installer needs into installer/payload/
// so the published npm tarball is self-contained.
// Run via `npm run prepack` (wired in package.json). Local dev bypass: the installer
// reads straight from the repo root when payload/ doesn't exist.

import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import {
  cpSync,
  existsSync,
  mkdirSync,
  rmSync,
} from 'node:fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, '..', '..');
const PAYLOAD = resolve(__dirname, '..', 'payload');

const files = [
  ['skills/video-watch/SKILL.md', 'SKILL.md'],
  ['video_analyze.py', 'video_analyze.py'],
  ['requirements.txt', 'requirements.txt'],
];

if (existsSync(PAYLOAD)) rmSync(PAYLOAD, { recursive: true, force: true });
mkdirSync(PAYLOAD, { recursive: true });

for (const [from, to] of files) {
  const src = join(REPO_ROOT, from);
  const dst = join(PAYLOAD, to);
  if (!existsSync(src)) {
    console.error(`Missing: ${src}`);
    process.exit(1);
  }
  cpSync(src, dst);
  console.log(`Bundled ${from} -> payload/${to}`);
}
console.log('Payload ready.');
