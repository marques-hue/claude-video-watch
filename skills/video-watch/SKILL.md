---
name: video-watch
description: Analyze and break down videos dropped into the conversation. Triggers when the user says "watch this video", "analyze this reel", "break down this video", "hook mine this", "what's the hook", "tear down this clip", or attaches/pastes a path to a .mp4, .mov, .webm, .mkv, or .m4v file.
---

# Video Watch

Claude does not natively ingest video files. This skill bridges that gap. When the user drops a video, run the pipeline at `{{SKILL_ROOT}}/video_analyze.py`, read the resulting JSON, and present the structured breakdown.

## V0.2 highlights

- **True native video for Gemini.** When `VIDEO_PROVIDER=gemini` the raw `.mp4` is uploaded through the google-genai File API and passed directly to `gemini-2.5-flash`. No ffmpeg, no Whisper, no frame extraction. Gemini reads native framerate and hears music + SFX + silence, so the `audio_cues` array is populated for free.
- **OCR-augmented frame analysis for Claude.** When `VIDEO_PROVIDER=anthropic` every extracted frame is run through easyocr before synthesis. Each beat carries an `OCR:` line with verbatim on-screen text, so Sonnet no longer hallucinates text from a downscaled JPEG.
- **Sonnet default for Claude.** Default Claude model is now `claude-sonnet-4-6` (override via `ANTHROPIC_MODEL` env). Faster and cheaper than Opus for the vision-heavy synthesis pass.

## When to invoke

Invoke this skill when any of these is true:
- The user literally says "watch this video", "analyze this reel", "break down this video", "hook mine this", "what's the hook of this", "tear down this clip"
- The user provides a path ending in `.mp4`, `.mov`, `.webm`, `.mkv`, `.m4v`
- The user drops a video attachment and asks for any kind of analysis

Do NOT invoke for:
- YouTube links or any URL (pipeline is local-file only for now — tell the user to download the file first)
- Audio-only files (use a different transcription flow)
- Requests that only want the transcript with no visual analysis (overkill)

## How to run it

1. Resolve the video path. If the user dropped a bash-style path (`/c/Users/...`) translate to Windows (`C:/Users/...`). Both forms usually work but the Python stdlib handles Windows paths better.
2. Confirm the file exists before spending any API budget.
3. Run the pipeline:

```bash
python {{SKILL_ROOT}}/video_analyze.py "<video-path>"
```

Optional flags:
- `--model small` (or `medium` / `large`) — more accurate transcription, slower on CPU. Default `base` is fine for clean audio. (Claude path only; Gemini native ignores it.)
- `--output path/to/out.json` — custom JSON output location. Default writes `<video>.analysis.json` next to the video.
- `--whisper-api` — swap local faster-whisper for OpenAI's API. Requires `OPENAI_API_KEY`. Useful when local transcription is too slow for a 5+ minute video. (Claude path only.)
- `--no-ocr` — skip easyocr pre-extraction on the Claude path. Faster but the model goes back to guessing on-screen text from downscaled frames.
- `--gemini-legacy` — force the Gemini provider through the old ffmpeg+Whisper+beats pipeline instead of the native File API. Only needed when the File API is unavailable.
- `--keep-work` — leaves frames + audio in a temp dir for debugging (Claude path only).

4. The pipeline prints a markdown report to stdout and writes full structured JSON to `<video>.analysis.json` next to the input.
5. Read the JSON file (more reliable than parsing stdout) and present the breakdown.

## How to present the result

Lead with the retention structure: hook (verbatim quote + technique + why it works), re-hook (named pattern-interrupt + what would happen without it), agitate, aha-moment, CTA (explicit/implicit/none). Then retention_mechanics (named patterns at timestamps), replication_checklist (3-7 copyable items), visual_beats, on_screen_text, emotional_arc.

The v2 schema (`schema_version: 2`) deepens hook/re_hook/cta from strings to objects. If the JSON is v1 (strings), render them as-is.

Keep voice aligned with Marques' brand rules from his global CLAUDE.md:
- No em dashes.
- No emoji.
- Calm, editorial, specific.
- Lowercase is fine but not required for breakdowns.

## Follow-up operations to offer

After the initial breakdown, offer one-line prompts:
- "Want me to rewrite the hook in your voice?"
- "Compare this to <competitor handle>'s last reel?"
- "Extract the 5-step retention structure as a reusable template?"
- "Turn the on-screen text into a caption draft?"

## Prerequisites

- `.env` file in the skill directory with `VIDEO_PROVIDER` and the matching API key. Written by `npx claude-video-install`.
- `ffmpeg` on PATH (required for the Claude path; not required when using Gemini native).
- Python dependencies installed: `pip install -r {{SKILL_ROOT}}/requirements.txt`. On first run easyocr will download its detection + recognition models (~100MB).

## Failure modes

- If the API key env var is missing, the pipeline exits with a clear error telling the user to run `npx claude-video-install`. Surface it and stop.
- If `ffmpeg` is missing on the Claude path, same. The Gemini native path does not need ffmpeg.
- If `faster-whisper` or `easyocr` isn't installed, suggest `pip install -r requirements.txt` from the skill's repo. If OCR specifically fails, pass `--no-ocr` to keep going; on-screen-text accuracy drops but the rest of the analysis is unaffected.
- If the Gemini File API times out or reports `FAILED`, retry once with `--gemini-legacy` to route through the ffmpeg+Whisper fallback.
- If the video is silent or very static on the Claude path, the pipeline falls back to fixed-interval frame sampling and the transcript will be empty. That is expected — the model still reads the frames via vision + OCR.
- If the pipeline returns `"(synthesis failed to return parseable JSON)"` in the summary, show the raw response from the `raw_response` field and offer a re-run.
