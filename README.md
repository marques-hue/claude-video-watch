# video-watch

> analyze short-form video locally. retention, hooks, on-screen text, beat-aligned frames and speech — in a single command from your agent of choice.

video-watch turns a local reel into a structured retention breakdown your next script can actually copy. point it at a `.mp4`, get back a JSON (and a human-readable markdown report) with:

- verbatim hook + timestamp + named technique
- re-hook, agitate, aha-moment, cta (explicit/implicit/none) — each with timestamp and quote
- named retention mechanics (pattern-interrupt, open-loop, numeric-claim, social-proof, pay-off)
- beat-aligned frames, speech, and on-screen text at the same timestamps
- a 3–7 item replication checklist you can paste into your next script

no upload. no hosted storage. no paid tier. your own gemini or claude api key.

---

## install

video-watch installs into all five of these agents:

- claude code
- cursor
- github copilot (vs code)
- opencode
- codex

### one command (recommended)

```bash
npx skills add marques-hue/claude-video-watch
```

detects every installed agent on your machine and writes video-watch into all of them. safe on windows (uses junctions, falls back to copy). ported from [videodb's distribution pattern](https://github.com/video-db/skills) — see credit at the bottom.

### branded installer (single-agent, more control)

```bash
npx video-watch-install             # interactive, detects installed agents
npx video-watch-install --agents all   # write to every detected agent
npx video-watch-install --agent cursor # just cursor
npx video-watch-install --global       # write to ~ instead of cwd
npx video-watch-install doctor         # check your environment is ready
npx video-watch-install update         # pull latest payload
npx video-watch-install remove         # uninstall
```

the installer prompts for your provider (gemini or anthropic), validates the api key prefix, masks it at entry, and runs `pip install -r requirements.txt` for you.

### per-agent install paths

| agent | install writes to |
|---|---|
| claude code | `~/.claude/skills/video-watch/` (junction to canonical `~/.agents/skills/video-watch/`) |
| cursor | `./.cursor/rules/video-watch.mdc` + payload in `.cursor/rules/video-watch/` |
| github copilot | `~/.copilot/skills/video-watch/` |
| opencode | `~/.config/opencode/skills/video-watch/` |
| codex | `~/.codex/skills/video-watch/` |

all five read the same `.env` and the same python script. a single install keeps every agent in sync.

### prereqs

- python 3.10+
- node 18+ (for the installer)
- ffmpeg on PATH (windows: `winget install Gyan.FFmpeg`, mac: `brew install ffmpeg`, linux: `sudo apt install ffmpeg`)
- one of: a gemini api key (free tier works) or an anthropic api key

---

## quickstart

once installed, tell any supported agent:

```
analyze C:/Users/me/Desktop/my-reel.mp4
```

or run the python directly:

```bash
python ~/.agents/skills/video-watch/video_analyze.py my-reel.mp4
```

the agent (or your terminal) produces `my-reel.analysis.json` plus a markdown report to stdout.

### example breakdown (30s reel)

input: a talking-head reel with a product demo cut.

output (condensed):

```json
{
  "schema_version": 2,
  "summary": "Creator pitches Claude Code as a replacement for Canva-style workflows, using a live dashboard demo as proof.",
  "hook": {
    "quote": "Canva is about to become obsolete and here's why.",
    "timestamp_seconds": 0.3,
    "technique": "contrarian-claim + open-loop",
    "why_it_works": "names a familiar brand being killed, forces the viewer to stay to learn the replacement."
  },
  "re_hook": {
    "timestamp_seconds": 7.97,
    "technique": "visual pattern-interrupt (tool-switch)",
    "what_would_happen_without_it": "viewer drop-off at 8s — three consecutive talking-head frames would read as a single static beat and scroll intent returns."
  },
  "cta": {
    "type": "implicit",
    "quote": "just go build the thing yourself.",
    "timestamp_seconds": 28.1
  },
  "retention_mechanics": [
    { "timestamp_seconds": 0.3, "mechanic": "contrarian-claim", "evidence": "\"about to become obsolete\"" },
    { "timestamp_seconds": 7.97, "mechanic": "pattern-interrupt (tool-switch)", "evidence": "cut from creator to claude dashboard" },
    { "timestamp_seconds": 14.2, "mechanic": "numeric-proof", "evidence": "\"in about 4 seconds\"" }
  ],
  "replication_checklist": [
    "open with a contrarian claim naming a brand your audience knows",
    "cut to a tool-switch at 6-8 seconds",
    "show a concrete numeric proof inside the first 15 seconds",
    "end on an implicit build-it-yourself cta, not a follow-me cta"
  ]
}
```

this is what "creator-useful" means. not a model summary of the video — a technique-by-technique structure you can copy.

---

## new in v2

### swipe file

every analysis appends the hook to `~/.agents/skills/video-watch/swipefile.jsonl`. search it later:

```bash
video-watch swipe --tag pattern-interrupt
video-watch swipe --search "you won't believe"
```

your personal hook library, grep-friendly, local-only.

### fingerprint your own back catalog

point it at a folder of your own past reels:

```bash
video-watch fingerprint ~/Desktop/my-reels/ --metrics views-and-saves.csv
```

aggregates retention mechanics across every video, cross-tabs against your success metric (a csv you maintain with `filename, views, saves`), and surfaces patterns you didn't consciously name. example output:

```
pattern-interrupt at 3-5s: 7 of your top 10 videos, 1 of your bottom 10
numeric-claim hooks: correlate with 2.3x median saves
implicit-cta: shows up in 9 of your top 10, 4 of your bottom 10
```

### compare mode

feed three reels, find the shared structure:

```bash
video-watch compare reel-a.mp4 reel-b.mp4 reel-c.mp4
```

output is a single markdown file that names the retention structure shared across all three and which shared element is most likely causal. this is the teardown you'd do by hand, done for you.

---

## new in v0.2

### true native video for gemini

gemini path no longer hacks around ffmpeg. the raw `.mp4` uploads directly through the google-genai File API, processes server-side, and gemini 2.5 flash or pro reads it at native framerate with full audio. no frame extraction. no whisper. no beat alignment. gemini hears music swells, sfx, silence, breath — the things a transcript strips out. populate `audio_cues` for free.

fallback: pass `--gemini-legacy` to route the Gemini path back through the old ffmpeg+Whisper+beats pipeline. useful when the File API is down or your key is region-locked.

### ocr-augmented frame analysis for claude

anthropic has no native video input yet, so the Claude path keeps the ffmpeg hack, but each extracted frame now runs through easyocr before synthesis. the on-screen text is handed to the model as a dedicated `OCR:` line per beat, verbatim. Sonnet stops hallucinating text from downscaled JPEGs; the `on_screen_text` array now matches what is actually on screen.

defaults flipped: Claude model is now `claude-sonnet-4-6` (set via `ANTHROPIC_MODEL` env). faster + cheaper than Opus for vision pass with no quality loss for this task. skip OCR with `--no-ocr`.

## how it works

**gemini path (native):**

1. raw video uploads to gemini File API via `client.files.upload`.
2. poll until state is `ACTIVE` (a few seconds for short reels, up to a couple minutes for longer).
3. pass the file handle and the system prompt to `gemini-2.5-flash` in one shot.
4. gemini returns the full schema v2 JSON. file is deleted after.

**claude path (ffmpeg + ocr + sonnet):**

1. ffmpeg extracts scene-change frames + 16khz mono audio.
2. faster-whisper (local, cpu, int8) transcribes with word-level timestamps. language auto-detected. vad filter on.
3. every frame is aligned to its speech window. frames downscaled to 1024px longest edge.
4. easyocr reads on-screen text from every frame before synthesis. attached to each beat as `beat.ocr_text`.
5. aligned beats (frame + speech + OCR text at the same timestamp) are sent to claude-sonnet-4-6. short videos single-pass; long videos chunked into 60s windows + meta-pass so `visual_beats` and `on_screen_text` arrays survive long content.
6. provider calls retry 3x with exponential backoff. partial progress preserved on fatal error.
7. the synthesis prompt asks for named techniques and replication-level insight, not meta-description.
8. json + markdown written to disk.

---

## docs

full docs: [video-watch.dev/docs](https://video-watch.dev/docs) *(placeholder)*

- [schema reference](https://video-watch.dev/docs/schema) — every field in the v2 analysis JSON
- [swipe + fingerprint + compare guide](https://video-watch.dev/docs/moats)
- [per-agent setup](https://video-watch.dev/docs/agents) — cursor mdc format, opencode skill frontmatter, codex sandbox approval, etc.

---

## credit

video-watch's multi-agent distribution model is adapted from videodb's [skills](https://github.com/video-db/skills) repository and the underlying [vercel-labs/skills](https://github.com/vercel-labs/skills) cli. videodb's single-command install that quietly writes a SKILL.md into whichever of claude code, cursor, opencode, codex, or github copilot happens to be on the machine is the distribution pattern we are porting. we are not building a videodb competitor. videodb owns server-side video infrastructure — ingest, editing, streaming, live capture — for developers wiring video pipelines into apps. video-watch stays on the creator side: a local file in, a retention and hook and on-screen-text breakdown out, no backend, no paid tier. credit where it is due: the multi-agent symlink-to-canonical-dir install pattern and the "universal `.agents/skills`" convention are theirs. the creator-analysis thesis and the output shape are ours.

---

## license

mit.
