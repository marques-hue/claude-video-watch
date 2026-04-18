# claude-video v0.0.1 Test Plan

Goal: before shipping to npm or filming the BIP reel, verify the pipeline produces output at or near "Gemini Pro" quality on a real short-form reel.

## What you're testing

A 3-way comparison on the SAME reel:

1. **Gemini web (baseline)** — upload video directly to gemini.google.com, let Google's pipeline handle everything
2. **claude-video + Gemini 1.5 Flash** — our pipeline, free tier, fast
3. **claude-video + Claude Opus 4.7** — our pipeline, paid, slowest but most detailed

We pick a reel you already know cold so you can judge which output is closest to ground truth.

## Pick a reel

Criteria:
- 30 to 60 seconds long (short-form, matches the tool's target use case)
- You know it cold — you've watched it enough to know the hook, re-hook, aha beat by memory
- Saved locally as `.mp4` (not a URL, not a share link)
- Has audible speech (silent reels test less of the pipeline)

Candidates:
- One of your own reels from batch 1 or teaser
- A competitor reel you've swipe-filed (Saakshi / joshcantcode / similar)
- Any reel with a clear structural hook so you can judge if the model found it

Save to a known path, e.g. `C:/Users/Marqu/Desktop/test-reel.mp4`.

## Step 1 — Gemini web baseline

1. Go to [gemini.google.com](https://gemini.google.com).
2. Click the `+` attachment, upload the reel.
3. Paste this prompt exactly:

```
Break down this video using the 5-step retention structure: hook (0-2s),
agitate (2-5s), re-hook (visual cut/overlay), tell them (value delivery),
aha + CTA. Also list visual beats with timestamps, any on-screen text,
audio cues, emotional arc, and the full transcript.

Return JSON only, no preamble:
{
  "summary": "",
  "hook": "",
  "agitate": "",
  "re_hook": "",
  "tell_them": "",
  "aha": "",
  "cta": "",
  "visual_beats": [{"t": 0.0, "description": "", "action": ""}],
  "on_screen_text": [{"t": 0.0, "text": ""}],
  "audio_cues": [],
  "emotional_arc": "",
  "transcript": ""
}
```

4. Screenshot the full output. Save to `C:/Users/Marqu/Desktop/test-gemini-web.json` (or paste into a file manually — whatever is fastest).

## Step 2 — claude-video + Gemini Flash

1. Check `~/.claude/skills/claude-video/.env` exists with:
   ```
   VIDEO_PROVIDER=gemini
   GEMINI_API_KEY=AIza...
   ```
   If not, run `node ~/Desktop/claude-video-watch/installer/bin/install.js` and pick Gemini.

2. Install Python deps (first time only):
   ```bash
   pip install -r ~/Desktop/claude-video-watch/requirements.txt
   ```

3. Run the pipeline:
   ```bash
   cd ~/Desktop/claude-video-watch
   python video_analyze.py "C:/Users/Marqu/Desktop/test-reel.mp4" --output test-gemini-flash.json
   ```

4. Output lands at `test-gemini-flash.json`. Markdown report prints to terminal.

## Step 3 — claude-video + Claude Opus

1. Re-run installer, pick Anthropic this time (reset option when prompted).
2. Paste your Anthropic API key from https://console.anthropic.com/settings/keys.
3. Run the pipeline again with a different output filename:
   ```bash
   python video_analyze.py "C:/Users/Marqu/Desktop/test-reel.mp4" --output test-claude-opus.json
   ```

## Step 4 — Diff the three

Open all three JSON files side by side. Score each on a 1 to 5 scale per dimension. Be honest — if claude-video is worse than Gemini web, we have to know.

| Dimension | Gemini Web | claude-video + Flash | claude-video + Opus |
|---|---|---|---|
| Hook accuracy (matches your ground truth) | /5 | /5 | /5 |
| Re-hook detection (catches the visual cut) | /5 | /5 | /5 |
| Aha moment framing | /5 | /5 | /5 |
| Visual beats detail (how many, how specific) | /5 | /5 | /5 |
| On-screen text OCR | /5 | /5 | /5 |
| Transcript accuracy | /5 | /5 | /5 |
| Overall usefulness (would you swipe-file from this?) | /5 | /5 | /5 |

## Step 5 — Decide

**Ship path A — claude-video passes the bar:**
- At least one of (Flash, Opus) scores within 1 point of Gemini web on every dimension.
- Ship. Fill npm metadata, create GitHub repo, `npm publish`, film BIP reel.

**Ship path B — claude-video is noticeably worse:**
- Gemini web beats both ours by 2+ points on any load-bearing dimension (hook, re-hook, aha).
- Do NOT ship yet. Possible fixes in order of impact:
  1. Upgrade Whisper model: change `--model base` to `--model medium` or `--model large-v3`. Bigger transcription quality hit than anything else.
  2. Lower scene threshold: `SCENE_THRESHOLD = 0.3` in `video_analyze.py` to `0.2` for more frames on fast-cut reels.
  3. Upgrade model: change `GEMINI_MODEL = "gemini-1.5-flash"` to `"gemini-1.5-pro"` (or 2.5 Pro if free tier allows). Trade: 2 req/min instead of 15.
  4. Prompt tune: add a few-shot example of one of your own reel breakdowns to the system prompt so the model learns your voice.

Re-test after each change. Ship when path A holds.

## Research findings worth knowing

From Google AI docs (searched 2026-04-18):

- **Gemini handles videos up to 1 hour** at default resolution, 3 hours at low res. Our original assumption of "5 minutes combined" was outdated.
- **Gemini samples video at 1 FPS natively** when you upload a video file directly. Our pipeline currently extracts scene-change frames manually and uploads them as images. For short reels this is probably similar or better (scene-change catches visual beats that 1 FPS misses). For long videos, native upload would be much more efficient.
- **~300 tokens per second of video** at default res, 100 tokens/sec at low res. A 60s reel is ~18k tokens — trivial.
- **File API supports up to 2GB files.** No practical size issue for reels.

### Implication for v0.2

A future `--native` flag could skip our ffmpeg + Whisper pipeline entirely on short clips and hand the `.mp4` straight to the Gemini File API. Pros: way less code, no local deps. Cons: loses our timestamp-aligned beat structure (the part that makes Claude Opus output so specific).

Not shipping this in v0.0.1. Worth revisiting after first real test results come in.

### Claude + video?

Anthropic does not have a native video input API as of 2026-04-18. Our current pipeline (frames + transcript → Claude) is the only way to feed video to Claude. This is part of why the skill exists.

## When to `/clear`

After Step 4 (the diff table). Before `/clear`, make sure you have:

- [ ] Scores filled in the comparison table
- [ ] The winning path (A or B) decided
- [ ] The three JSON files saved somewhere you can find them later
- [ ] The reel path documented so we can re-test after any changes

Then `/clear` and hand me the table + path decision. I'll act on it without needing the full history.

## Failure modes worth anticipating

- **`faster-whisper` download takes forever on first run.** Normal — Whisper base model is ~140MB, large-v3 is ~3GB. Only happens once.
- **ffmpeg scene detection returns few frames.** The pipeline falls back to fixed-interval sampling. Expected behavior for static talking-head reels.
- **`(synthesis failed to return parseable JSON)`.** Rerun. If it happens twice on the same video, the model response is malformed — capture the `raw_response` field and flag it.
- **Gemini rate limit (15 req/min on Flash).** A single reel is 1 to 6 API calls depending on length. If you're hitting limits, you're running tests too fast. Wait 60s between runs.
- **Anthropic OAuth confusion.** Claude Code Max plan does not give you an API key. You need a real key from console.anthropic.com with its own billing. Spend is usually under $1 per reel on Opus.
