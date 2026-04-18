# claude-video

Teach Claude Code to watch videos. Drop a `.mp4`, `.mov`, `.webm`, `.mkv`, or `.m4v` into the conversation and Claude returns a structured breakdown: hook, agitate, re-hook, tell-them, aha, CTA. Plus visual beats, on-screen text, audio cues, and the full transcript.

Built for short-form creators who want to hook-mine, swipe-file, and reverse-engineer retention structure.

## Install

```bash
npx claude-video-install
```

The installer asks which provider you want (Gemini or Anthropic), writes your key to a local `.env`, and drops the skill into `~/.claude/skills/claude-video/`.

Gemini is the default pick: free tier, 15 requests per minute, enough for most reels.

## Requirements

- Python 3.10 or newer
- `ffmpeg` on PATH ([install](https://ffmpeg.org/download.html))
- Node 18+ (for the installer only)
- An API key from one of:
  - Google AI Studio — https://aistudio.google.com/app/apikey (free)
  - Anthropic Console — https://console.anthropic.com/settings/keys (paid)

Python dependencies:

```bash
pip install -r ~/.claude/skills/claude-video/requirements.txt
```

## Usage

After install, restart Claude Code. Then in any conversation:

> watch this video: `/path/to/clip.mp4`

Or:

> break down this reel, what's the hook

The skill picks up automatically. The pipeline runs four stages:

1. **Extract** audio (16kHz mono WAV) and frames (scene-change detection) via ffmpeg
2. **Transcribe** locally with faster-whisper
3. **Align** transcript words to each frame so visuals and speech stay in sync
4. **Synthesize** with Gemini or Claude (your pick) into a structured JSON report

Output lands next to the video as `<video>.analysis.json` and renders a markdown summary in the Claude Code conversation.

## Update

```bash
npx claude-video-install
```

Detects the existing install and offers to update files (keeping your `.env`) or wipe and reinstall.

## Providers

| Provider | Cost | Speed | Quality | When to pick |
|---|---|---|---|---|
| Gemini 2.5 Flash | free (15 req/min) | fast | matches Pro on short-form | default |
| Gemini 2.5 Pro | free tier | slow | cleaner prose | long-form or client deliverables |
| Claude Opus 4.7 | ~$0.20 / 90s video | slow | verbatim hook extraction, names gear in beats | when raw-source fidelity matters |

Switching later: rerun `npx claude-video-install`, pick reset, choose the other provider.

## Troubleshooting

- `ffmpeg not found on PATH` — install ffmpeg, restart your terminal
- `faster-whisper is not installed` — run the pip install command above
- Silent video or no transcript — expected. The frames still get analyzed.
- `(synthesis failed to return parseable JSON)` — rerun, usually a cold-cache blip

## License

MIT. See [LICENSE](LICENSE).

## Who made this

[@marquessystems](https://instagram.com/marquessystems) — solo founder, Henderson NV. Builds tools for freelancers escaping scope creep. See the Stability Score freelancer diagnostic at [stabilityscore.app](https://stabilityscore.app).
