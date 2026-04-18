# claude-video installer

Interactive installer for the `claude-video` Claude Code skill.

## Local test (before publishing)

```bash
cd installer
npm install
node bin/install.js
```

The installer reads SKILL.md, video_analyze.py, and requirements.txt from the
repo root when `payload/` does not exist yet, so you can iterate without
running the bundle step.

## Publish flow (later)

```bash
cd installer
npm install
npm run prepack   # copies repo files into installer/payload/
npm publish
```

Users then run:

```bash
npx claude-video-install
```

## What it does

1. Prompts for provider: Gemini (free tier) or Anthropic (paid).
2. Prompts for API key. Validates prefix (`AIza` / `sk-ant-`).
3. Copies skill files to `~/.claude/skills/claude-video/`.
4. Writes `.env` with the key. chmod 600 on unix, user-profile ACL on Windows.
5. Rewrites the hardcoded Desktop path in SKILL.md to the install location.

Re-running detects existing install and offers:
- Update skill files (keeps `.env`)
- Reinstall from scratch (wipes `.env`)
- Cancel
