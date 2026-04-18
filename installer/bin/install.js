#!/usr/bin/env node
// claude-video installer
// Copies skill payload to ~/.claude/skills/claude-video, prompts for provider + key,
// writes .env with chmod 600 on unix. Windows relies on user-profile ACL.

import * as p from '@clack/prompts';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { homedir, platform } from 'node:os';
import {
  existsSync,
  mkdirSync,
  cpSync,
  writeFileSync,
  readFileSync,
  chmodSync,
  rmSync,
} from 'node:fs';

// --- Branding ---------------------------------------------------------------
const PKG_VERSION = '0.0.1';
const USE_COLOR = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code, s) => (USE_COLOR ? `\x1b[${code}m${s}\x1b[0m` : s);
const terracotta = (s) => paint('38;2;210;93;56', s); // #D25D38
const gold = (s) => paint('38;2;221;163;40', s); // #DDA328
const dim = (s) => paint('2;37', s);
const slate = (s) => paint('38;2;180;170;160', s);

const LOGO = [
  '       _                 _            _     _            ',
  '   ___| | __ _ _   _  __| | ___    __(_) __| | ___  ___  ',
  '  / __| |/ _` | | | |/ _` |/ _ \\  / _` |/ _` |/ _ \\/ _ \\ ',
  ' | (__| | (_| | |_| | (_| |  __/ | (_| | (_| |  __/ (_) |',
  '  \\___|_|\\__,_|\\__,_|\\__,_|\\___|  \\__,_|\\__,_|\\___|\\___/ ',
];

function printBanner() {
  const line = '─'.repeat(57);
  console.log('');
  for (const l of LOGO) console.log(terracotta(l));
  console.log('');
  console.log(
    `  ${slate('teach Claude Code to watch videos.')}` +
      '          ' +
      gold(`v${PKG_VERSION}`),
  );
  console.log(`  ${dim(line)}`);
  console.log('');
}

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Payload layout (bundled at publish time, see scripts/bundle-payload.js):
//   <installer>/payload/
//     SKILL.md
//     video_analyze.py
//     requirements.txt
// For local dev runs, payload lives one dir up from installer/ in the repo root,
// so fall back to that if payload/ doesn't exist yet.
const PAYLOAD_DIR = resolveFirstExisting([
  join(__dirname, '..', 'payload'),
  resolve(__dirname, '..', '..'),
]);

const SKILL_NAME = 'claude-video';
const SKILL_DST = join(homedir(), '.claude', 'skills', SKILL_NAME);

function resolveFirstExisting(paths) {
  for (const candidate of paths) {
    if (existsSync(candidate)) return candidate;
  }
  return paths[paths.length - 1];
}

function cancelIf(value, msg = 'Aborted.') {
  if (p.isCancel(value)) {
    p.cancel(msg);
    process.exit(0);
  }
}

function maskKey(key) {
  if (!key || key.length < 10) return '****';
  return `${key.slice(0, 6)}…${key.slice(-4)}`;
}

async function main() {
  console.clear();
  printBanner();
  p.intro(terracotta('● setup'));

  // --- Existing install detection ------------------------------------------
  let keepEnv = false;
  if (existsSync(SKILL_DST)) {
    const action = await p.select({
      message: `${SKILL_NAME} already installed at ${SKILL_DST}`,
      options: [
        { value: 'update', label: 'Update skill files', hint: 'keeps existing .env' },
        { value: 'reset', label: 'Reinstall from scratch', hint: 'wipes .env too' },
        { value: 'abort', label: 'Cancel' },
      ],
    });
    cancelIf(action);
    if (action === 'abort') {
      p.outro('No changes made.');
      process.exit(0);
    }
    keepEnv = action === 'update';
  }

  // --- Provider + key (skip if keeping env) --------------------------------
  let provider;
  let apiKey;
  if (!keepEnv) {
    provider = await p.select({
      message: 'Pick video analysis provider',
      options: [
        {
          value: 'gemini',
          label: 'Gemini (recommended)',
          hint: 'free tier, 15 req/min, matches Claude quality',
        },
        {
          value: 'anthropic',
          label: 'Anthropic',
          hint: 'paid ~$0.20/video, verbatim hook extraction',
        },
      ],
    });
    cancelIf(provider);

    const keyLabel = provider === 'gemini' ? 'GEMINI_API_KEY' : 'ANTHROPIC_API_KEY';
    const keyPrefix = provider === 'gemini' ? 'AIza' : 'sk-ant-';
    const signupUrl =
      provider === 'gemini'
        ? 'https://aistudio.google.com/app/apikey'
        : 'https://console.anthropic.com/settings/keys';

    if (provider === 'anthropic') {
      p.note(
        'Claude Code Max plan OAuth is NOT a valid API key.\n' +
          'You need a real API key (starts with sk-ant-).\n' +
          `Get one: ${signupUrl}`,
        'Heads up',
      );
    } else {
      p.note(`Get a key: ${signupUrl}`, 'Need a key?');
    }

    apiKey = await p.password({
      message: `Paste ${keyLabel}`,
      validate: (v) => {
        if (!v) return 'Required.';
        const trimmed = v.trim();
        if (!trimmed.startsWith(keyPrefix))
          return `Expected key to start with "${keyPrefix}".`;
        if (trimmed.length < 20) return 'Key looks too short.';
        return undefined;
      },
    });
    cancelIf(apiKey);
    apiKey = apiKey.trim();
  }

  // --- Write files ---------------------------------------------------------
  const s = p.spinner();
  s.start('Installing skill files');

  // Preserve .env content if updating
  const envPath = join(SKILL_DST, '.env');
  const preservedEnv =
    keepEnv && existsSync(envPath) ? readFileSync(envPath, 'utf8') : null;

  if (existsSync(SKILL_DST) && !keepEnv) {
    rmSync(SKILL_DST, { recursive: true, force: true });
  }
  mkdirSync(SKILL_DST, { recursive: true });

  // Copy SKILL.md (from payload/ or repo skills/video-watch/ for local dev)
  const skillMdCandidates = [
    join(PAYLOAD_DIR, 'SKILL.md'),
    join(PAYLOAD_DIR, 'skills', 'video-watch', 'SKILL.md'),
  ];
  const skillMdSrc = skillMdCandidates.find(existsSync);
  if (!skillMdSrc) throw new Error('SKILL.md not found in payload.');

  // Rewrite hardcoded Desktop path in SKILL.md to the install location
  const skillMd = readFileSync(skillMdSrc, 'utf8')
    .replaceAll(
      'C:/Users/Marqu/Desktop/claude-video-watch',
      SKILL_DST.replaceAll('\\', '/'),
    )
    .replace(/^name:\s*.+$/m, `name: ${SKILL_NAME}`);
  writeFileSync(join(SKILL_DST, 'SKILL.md'), skillMd);

  // Copy script + requirements
  for (const file of ['video_analyze.py', 'requirements.txt']) {
    const src = join(PAYLOAD_DIR, file);
    if (!existsSync(src)) throw new Error(`Missing payload file: ${file}`);
    cpSync(src, join(SKILL_DST, file));
  }

  // .gitignore (in case user re-git-inits the skill dir)
  writeFileSync(
    join(SKILL_DST, '.gitignore'),
    '.env\n__pycache__/\n*.pyc\n*.analysis.json\n',
  );

  // .env
  if (preservedEnv) {
    writeFileSync(envPath, preservedEnv, { mode: 0o600 });
  } else {
    const keyLabel = provider === 'gemini' ? 'GEMINI_API_KEY' : 'ANTHROPIC_API_KEY';
    const envLines = [
      `# Written by claude-video-install on ${new Date().toISOString()}`,
      `VIDEO_PROVIDER=${provider}`,
      `${keyLabel}=${apiKey}`,
    ];
    if (provider === 'gemini') {
      envLines.push('GEMINI_MODEL=gemini-2.5-flash');
    }
    envLines.push('');
    const envBody = envLines.join('\n');
    writeFileSync(envPath, envBody, { mode: 0o600 });
  }
  if (platform() !== 'win32') {
    try {
      chmodSync(envPath, 0o600);
    } catch {
      /* best-effort */
    }
  }

  s.stop('Skill installed');

  // --- Summary -------------------------------------------------------------
  const summaryLines = [
    `Location: ${SKILL_DST}`,
    keepEnv ? 'Env: preserved existing .env' : `Provider: ${provider}`,
    keepEnv ? '' : `Key: ${maskKey(apiKey)}`,
    '',
    'Next:',
    `  pip install -r "${join(SKILL_DST, 'requirements.txt')}"`,
    '  ffmpeg must be on PATH',
    '  Restart Claude Code, then drop a video into the conversation',
  ]
    .filter(Boolean)
    .join('\n');
  p.note(summaryLines, 'Done');
  p.outro('claude-video ready.');
}

main().catch((err) => {
  p.cancel(`Install failed: ${err.message}`);
  console.error(err);
  process.exit(1);
});
