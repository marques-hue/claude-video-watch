"""
video_analyze.py

Pipeline that teaches Claude Code to "watch" a video. Four stages:

    Stage 1. Extract audio (16kHz mono WAV) and frames (scene-change detection)
             using ffmpeg via subprocess. No moviepy.
    Stage 2. Transcribe the audio locally with faster-whisper (or via the
             OpenAI Whisper API if --whisper-api is passed).
    Stage 3. Align transcript words to each extracted frame so every frame
             has the speech that happened in its window. This is what makes
             synthesis actually useful: Claude sees "at 0:14 speaker says X
             while visual shows Y" instead of two disconnected streams.
    Stage 4. Multimodal synthesis with Claude Opus 4.7. Base64-encoded frames
             plus aligned speech go into the messages payload. Long videos
             are chunked into 60s windows with a final meta-pass. A stable
             system prompt is cached with cache_control so repeated runs
             within 5 minutes hit the prompt cache.

Output: structured JSON matching the schema in the --help text, plus a
pretty-printed markdown report to stdout.

Usage:
    python video_analyze.py <video> [--model base|small|medium|large]
                                    [--output path]
                                    [--whisper-api]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Load .env from this script's directory (written by the installer).
# Silent no-op if python-dotenv isn't installed or no .env exists.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Stage 1: ffmpeg extraction
# --------------------------------------------------------------------------- #

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Scene-change threshold. 0.3 is a reasonable default for talking-head reels,
# tutorial clips, and mid-energy social content. Tune lower (0.15-0.2) for
# very static footage, higher (0.4-0.5) for rapid-cut content.
SCENE_THRESHOLD = 0.3
MIN_SCENE_FRAMES = 3  # if scene detection returns fewer, we fall back to fixed interval
FALLBACK_INTERVAL_SEC = 2.0


def run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command and return the completed process.

    We capture stderr because ffmpeg writes informational output there, and
    the showinfo filter emits timestamps we need to parse.
    """
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
    )


def probe_duration(video_path: Path) -> float:
    """Get the video duration in seconds using ffprobe."""
    result = run([
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ])
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def extract_audio(video_path: Path, out_path: Path) -> None:
    """Extract audio as 16kHz mono WAV - Whisper's native format.

    Going straight to WAV at Whisper's sample rate avoids a lossy MP3 round
    trip and shaves a few seconds off the pipeline for long videos.
    """
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vn",                 # no video
        "-ac", "1",            # mono
        "-ar", "16000",        # 16kHz
        "-c:a", "pcm_s16le",   # 16-bit PCM
        str(out_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\n{result.stderr}")


def extract_frames_scene(video_path: Path, frames_dir: Path) -> list[tuple[int, float, Path]]:
    """Extract frames using scene-change detection.

    Returns a list of (index, timestamp_sec, frame_path). The timestamp is
    the frame's offset in the source video, parsed from ffmpeg's showinfo
    filter output on stderr.

    If fewer than MIN_SCENE_FRAMES come back (static video, silent product
    demo, anything with a single visual), we fall back to fixed-interval
    sampling so Claude still gets visual coverage.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Scene detection: select frames where the scene-change score exceeds
    # SCENE_THRESHOLD, then pipe through showinfo so we can recover timestamps.
    # We use a temp pattern and rename afterward because we need the real
    # timestamps before we can name files properly.
    temp_pattern = frames_dir / "raw_%04d.jpg"
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-vsync", "vfr",
        "-q:v", "3",           # JPEG quality (2-5 is the sweet spot)
        str(temp_pattern),
    ]
    result = run(cmd)

    # Parse timestamps from showinfo stderr. Lines look like:
    #   [Parsed_showinfo_1 @ 0x...] n:0 pts:... pts_time:1.42 ...
    ts_pattern = re.compile(r"pts_time:([\d.]+)")
    timestamps = [float(m.group(1)) for m in ts_pattern.finditer(result.stderr)]

    # Collect the raw frames that actually got written.
    raw_frames = sorted(frames_dir.glob("raw_*.jpg"))

    # If scene detection was too sparse, wipe and fall back to fixed interval.
    if len(raw_frames) < MIN_SCENE_FRAMES:
        for f in raw_frames:
            f.unlink()
        return extract_frames_interval(video_path, frames_dir)

    # Pair raw frames with timestamps. If the counts disagree (rare, happens
    # when showinfo buffering trims output), we fall back to interval naming.
    if len(raw_frames) != len(timestamps):
        timestamps = [i * FALLBACK_INTERVAL_SEC for i in range(len(raw_frames))]

    results: list[tuple[int, float, Path]] = []
    for i, (raw, ts) in enumerate(zip(raw_frames, timestamps), start=1):
        final_name = f"frame_{i:04d}_t={ts:.2f}.jpg"
        final_path = frames_dir / final_name
        raw.rename(final_path)
        results.append((i, ts, final_path))
    return results


def extract_frames_interval(video_path: Path, frames_dir: Path) -> list[tuple[int, float, Path]]:
    """Fallback frame extractor: one frame every FALLBACK_INTERVAL_SEC seconds."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    temp_pattern = frames_dir / "int_%04d.jpg"
    fps = 1.0 / FALLBACK_INTERVAL_SEC
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-q:v", "3",
        str(temp_pattern),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed:\n{result.stderr}")

    raw_frames = sorted(frames_dir.glob("int_*.jpg"))
    results: list[tuple[int, float, Path]] = []
    for i, raw in enumerate(raw_frames, start=1):
        ts = (i - 1) * FALLBACK_INTERVAL_SEC
        final_name = f"frame_{i:04d}_t={ts:.2f}.jpg"
        final_path = frames_dir / final_name
        raw.rename(final_path)
        results.append((i, ts, final_path))
    return results


# --------------------------------------------------------------------------- #
# Stage 2: transcription
# --------------------------------------------------------------------------- #

@dataclass
class Word:
    word: str
    start: float
    end: float


@dataclass
class Transcript:
    text: str
    words: list[Word] = field(default_factory=list)


def transcribe_local(audio_path: Path, model_size: str) -> Transcript:
    """Transcribe with faster-whisper, running on CPU by default.

    We request word-level timestamps so Stage 3 can zip them to frames.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -r requirements.txt"
        ) from e

    # int8 CPU inference is the fastest workable combo on a dev laptop.
    # Users with NVIDIA GPUs can edit this line to "cuda" / "float16".
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        vad_filter=False,
        language="en",
    )

    words: list[Word] = []
    text_parts: list[str] = []
    for seg in segments:
        text_parts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                words.append(Word(word=w.word.strip(), start=w.start, end=w.end))
    return Transcript(text=" ".join(text_parts).strip(), words=words)


def transcribe_api(audio_path: Path) -> Transcript:
    """Transcribe via OpenAI's Whisper API. Optional path, requires openai pkg."""
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai package required for --whisper-api. Run: pip install openai"
        ) from e

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY env var not set")

    client = OpenAI(api_key=api_key)
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

    words: list[Word] = []
    for w in (getattr(response, "words", None) or []):
        words.append(Word(
            word=w["word"] if isinstance(w, dict) else w.word,
            start=w["start"] if isinstance(w, dict) else w.start,
            end=w["end"] if isinstance(w, dict) else w.end,
        ))
    text = getattr(response, "text", "") or ""
    return Transcript(text=text.strip(), words=words)


# --------------------------------------------------------------------------- #
# Stage 3: timestamp alignment
# --------------------------------------------------------------------------- #

@dataclass
class Beat:
    """One extracted frame plus the speech spoken in its window.

    A beat is the atomic unit we hand to Claude. Each one represents a
    moment in the video where we have both a visual and (possibly) some
    audio context. The synthesis prompt iterates over beats in order.
    """
    t: float
    frame_path: Path
    speech: str


def build_beats(
    frames: list[tuple[int, float, Path]],
    transcript: Transcript,
    video_duration: float,
) -> list[Beat]:
    """Zip frames to transcript words by timestamp window.

    For each frame at time t, the window extends until the next frame
    (or end of video for the last frame). All words whose start falls in
    [t, next_t) belong to that beat.
    """
    beats: list[Beat] = []
    for i, (_idx, ts, path) in enumerate(frames):
        next_ts = frames[i + 1][1] if i + 1 < len(frames) else video_duration or (ts + 5)
        window_words = [
            w.word for w in transcript.words
            if ts <= w.start < next_ts
        ]
        speech = " ".join(window_words).strip()
        beats.append(Beat(t=ts, frame_path=path, speech=speech))
    return beats


# --------------------------------------------------------------------------- #
# Stage 4: multimodal synthesis (Anthropic or Gemini)
# --------------------------------------------------------------------------- #

MODEL = "claude-opus-4-7"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

SYSTEM_PROMPT = """You are a video analyst for a content creator who produces short-form social reels (TikTok, Instagram Reels, YouTube Shorts).

Your job is to reverse-engineer a video into its structural beats so the creator can hook-mine, swipe-file, and study retention structure.

For every video you receive a sequence of timestamped beats. Each beat includes a still frame from the video and the words spoken during that frame's window. Treat them as synchronized: the visual and the speech are happening at the same moment.

Produce a structured JSON analysis matching this schema exactly:

{
  "summary": "one-paragraph overall description",
  "hook": "the first 0-2 seconds - what's the unhinged hook",
  "agitate": "2-5 seconds - the pain agitation beat",
  "re_hook": "the bracketed visual stage direction / reset moment",
  "tell_them": "the value delivery middle",
  "aha": "the payoff / aha moment",
  "cta": "any mid-roll or end CTA present",
  "visual_beats": [
    {"t": 0.0, "description": "what's on screen", "action": "what's happening"}
  ],
  "on_screen_text": [
    {"t": 0.0, "text": "extracted via OCR - read any text you see in the frames"}
  ],
  "audio_cues": ["music genre, sfx, tone markers"],
  "emotional_arc": "how the emotional register shifts",
  "transcript": "full plain transcript"
}

Rules:
- Read on-screen text directly from the frames. You have native OCR via vision.
- If a field is not present (no CTA, no clear re-hook), return an empty string or empty array.
- Do not invent content. If a beat is ambiguous, say so in its description.
- No emojis anywhere in your output.
- No em dashes. Use commas, periods, or parentheses.
- Return ONLY valid JSON. No preamble, no markdown code fences."""


CHUNK_SYSTEM_PROMPT = """You are a video analyst processing one segment of a longer video.

You receive timestamped beats from a single 60-second window. Each beat has a frame and the words spoken during that frame's window. Treat them as synchronized.

Produce a compact JSON summary of this window only:

{
  "window_start": 0.0,
  "window_end": 60.0,
  "segment_summary": "one paragraph",
  "visual_beats": [{"t": 0.0, "description": "what's on screen", "action": "what's happening"}],
  "on_screen_text": [{"t": 0.0, "text": "any text visible in frames"}],
  "speech": "the words spoken in this window",
  "emotional_register": "neutral | excited | angry | calm | authoritative | vulnerable | etc"
}

Rules:
- Read on-screen text directly from the frames (native vision OCR).
- No emojis, no em dashes.
- Return ONLY valid JSON. No markdown, no preamble."""


META_SYSTEM_PROMPT = """You are combining per-window summaries of a long video into one final structured analysis.

You receive an ordered list of window summaries plus the full transcript. Synthesize them into the final schema:

{
  "summary": "one-paragraph overall description",
  "hook": "the first 0-2 seconds",
  "agitate": "2-5 seconds - pain agitation",
  "re_hook": "bracketed visual stage direction / reset moment",
  "tell_them": "value delivery middle",
  "aha": "payoff / aha moment",
  "cta": "any CTA present",
  "visual_beats": [{"t": 0.0, "description": "...", "action": "..."}],
  "on_screen_text": [{"t": 0.0, "text": "..."}],
  "audio_cues": ["music, sfx, tone markers"],
  "emotional_arc": "how the emotional register shifts across the whole video",
  "transcript": "full plain transcript"
}

Rules:
- Preserve every visual_beat and on_screen_text entry from the window summaries, merged in timestamp order.
- No emojis, no em dashes.
- Return ONLY valid JSON."""


def encode_frame(frame_path: Path) -> dict[str, Any]:
    """Encode a frame as an Anthropic image content block (base64 JPEG)."""
    with open(frame_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": b64,
        },
    }


def build_beat_blocks(beats: list[Beat]) -> list[dict[str, Any]]:
    """Turn beats into an interleaved content array for the Anthropic API.

    Each beat becomes: a header text block, the image, and the speech block.
    Keeping the structure identical across requests helps prompt caching.
    """
    blocks: list[dict[str, Any]] = []
    for i, beat in enumerate(beats, start=1):
        header = f"--- Beat {i} at t={beat.t:.2f}s ---"
        blocks.append({"type": "text", "text": header})
        blocks.append(encode_frame(beat.frame_path))
        speech_line = beat.speech if beat.speech else "(no speech in this window)"
        blocks.append({"type": "text", "text": f"Speech: {speech_line}"})
    return blocks


def call_claude_single(
    beats: list[Beat],
    transcript: Transcript,
    anthropic_client,
) -> dict[str, Any]:
    """One-shot synthesis for short videos."""
    content: list[dict[str, Any]] = []
    content.append({
        "type": "text",
        "text": f"Full transcript (for reference):\n{transcript.text}\n\nBeats follow:",
    })
    content.extend(build_beat_blocks(beats))
    content.append({
        "type": "text",
        "text": "Return the JSON analysis now.",
    })

    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": content}],
    )
    return parse_response(response, transcript)


def call_claude_chunked(
    beats: list[Beat],
    transcript: Transcript,
    anthropic_client,
) -> dict[str, Any]:
    """Two-pass synthesis for long videos.

    Chunk beats into 60-second windows, summarize each, then do a meta-pass
    that combines the summaries into the final schema. This avoids blowing
    through token budgets on videos with dozens of frames.
    """
    window_size = 60.0
    if not beats:
        return empty_analysis(transcript)

    # Bucket beats into 60-second windows keyed by floor(t / 60).
    buckets: dict[int, list[Beat]] = {}
    for b in beats:
        key = int(b.t // window_size)
        buckets.setdefault(key, []).append(b)

    window_summaries: list[dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        window_beats = buckets[key]
        start = key * window_size
        end = start + window_size
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": f"Window: {start:.2f}s to {end:.2f}s",
        }]
        content.extend(build_beat_blocks(window_beats))
        content.append({"type": "text", "text": "Return the window JSON now."})

        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=[{
                "type": "text",
                "text": CHUNK_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        )
        window_json = extract_json(response_text(response))
        if window_json is None:
            window_json = {
                "window_start": start,
                "window_end": end,
                "segment_summary": "(parse failed)",
                "visual_beats": [],
                "on_screen_text": [],
                "speech": " ".join(b.speech for b in window_beats),
                "emotional_register": "unknown",
            }
        window_summaries.append(window_json)

    # Meta-pass: combine windows into final schema. No images here, just the
    # structured window data plus the full transcript.
    meta_content = [{
        "type": "text",
        "text": (
            "Full transcript:\n" + transcript.text +
            "\n\nWindow summaries (ordered):\n" +
            json.dumps(window_summaries, indent=2) +
            "\n\nReturn the final JSON analysis now."
        ),
    }]
    meta_response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{
            "type": "text",
            "text": META_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": meta_content}],
    )
    return parse_response(meta_response, transcript)


def response_text(response) -> str:
    """Extract concatenated text from an Anthropic response."""
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Gemini synthesis path
# --------------------------------------------------------------------------- #

def _gemini_beat_parts(beats: list[Beat]) -> list[Any]:
    """Build a Gemini content list from beats: header text, PIL Image, speech text."""
    try:
        import PIL.Image
    except ImportError as e:
        raise RuntimeError("Pillow is required. Run: pip install -r requirements.txt") from e

    parts: list[Any] = []
    for i, beat in enumerate(beats, start=1):
        parts.append(f"--- Beat {i} at t={beat.t:.2f}s ---")
        parts.append(PIL.Image.open(beat.frame_path))
        speech_line = beat.speech if beat.speech else "(no speech in this window)"
        parts.append(f"Speech: {speech_line}")
    return parts


def call_gemini_single(
    beats: list[Beat],
    transcript: Transcript,
    gemini_client,
) -> dict[str, Any]:
    """One-shot synthesis for short videos via Gemini."""
    content: list[Any] = [
        SYSTEM_PROMPT,
        f"Full transcript (for reference):\n{transcript.text}\n\nBeats follow:",
    ]
    content.extend(_gemini_beat_parts(beats))
    content.append("Return the JSON analysis now.")

    response = gemini_client.generate_content(
        content,
        generation_config={
            "response_mime_type": "application/json",
            "max_output_tokens": 16000,
        },
    )
    return parse_gemini_response(response, transcript)


def call_gemini_chunked(
    beats: list[Beat],
    transcript: Transcript,
    gemini_client,
) -> dict[str, Any]:
    """Two-pass synthesis for long videos via Gemini. Mirrors the Claude flow."""
    window_size = 60.0
    if not beats:
        return empty_analysis(transcript)

    buckets: dict[int, list[Beat]] = {}
    for b in beats:
        key = int(b.t // window_size)
        buckets.setdefault(key, []).append(b)

    window_summaries: list[dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        window_beats = buckets[key]
        start = key * window_size
        end = start + window_size
        content: list[Any] = [
            CHUNK_SYSTEM_PROMPT,
            f"Window: {start:.2f}s to {end:.2f}s",
        ]
        content.extend(_gemini_beat_parts(window_beats))
        content.append("Return the window JSON now.")

        response = gemini_client.generate_content(
            content,
            generation_config={
                "response_mime_type": "application/json",
                "max_output_tokens": 4000,
            },
        )
        window_json = extract_json(gemini_response_text(response))
        if window_json is None:
            window_json = {
                "window_start": start,
                "window_end": end,
                "segment_summary": "(parse failed)",
                "visual_beats": [],
                "on_screen_text": [],
                "speech": " ".join(b.speech for b in window_beats),
                "emotional_register": "unknown",
            }
        window_summaries.append(window_json)

    # Meta-pass: text-only combine into final schema.
    meta_content = [
        META_SYSTEM_PROMPT,
        "Full transcript:\n" + transcript.text +
        "\n\nWindow summaries (ordered):\n" +
        json.dumps(window_summaries, indent=2) +
        "\n\nReturn the final JSON analysis now.",
    ]
    meta_response = gemini_client.generate_content(
        meta_content,
        generation_config={
            "response_mime_type": "application/json",
            "max_output_tokens": 16000,
        },
    )
    return parse_gemini_response(meta_response, transcript)


def gemini_response_text(response) -> str:
    """Extract text from a Gemini response, robust to blocked / empty candidates."""
    try:
        return response.text or ""
    except Exception:
        # If response.text raised (safety block, no candidates), walk parts manually.
        parts: list[str] = []
        for cand in getattr(response, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        return "\n".join(parts)


def parse_gemini_response(response, transcript: Transcript) -> dict[str, Any]:
    """Parse Gemini JSON response, filling transcript if missing."""
    text = gemini_response_text(response)
    data = extract_json(text)
    if data is None:
        return {
            "summary": "(synthesis failed to return parseable JSON)",
            "hook": "", "agitate": "", "re_hook": "",
            "tell_them": "", "aha": "", "cta": "",
            "visual_beats": [], "on_screen_text": [],
            "audio_cues": [], "emotional_arc": "",
            "transcript": transcript.text,
            "raw_response": text,
        }
    if not data.get("transcript"):
        data["transcript"] = transcript.text
    return data


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a text blob.

    Claude is prompted to return raw JSON, but we guard against accidental
    markdown fences or preamble.
    """
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # Find the first balanced-ish top-level object.
    start = text.find("{")
    if start == -1:
        return None
    # Try progressively shorter suffixes to recover from trailing garbage.
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


def parse_response(response, transcript: Transcript) -> dict[str, Any]:
    """Parse Claude's JSON response, filling in transcript if missing."""
    text = response_text(response)
    data = extract_json(text)
    if data is None:
        return {
            "summary": "(synthesis failed to return parseable JSON)",
            "hook": "", "agitate": "", "re_hook": "",
            "tell_them": "", "aha": "", "cta": "",
            "visual_beats": [], "on_screen_text": [],
            "audio_cues": [], "emotional_arc": "",
            "transcript": transcript.text,
            "raw_response": text,
        }
    # Make sure the transcript is always populated, even if Claude omitted it.
    if not data.get("transcript"):
        data["transcript"] = transcript.text
    return data


def empty_analysis(transcript: Transcript) -> dict[str, Any]:
    return {
        "summary": "(no beats extracted)",
        "hook": "", "agitate": "", "re_hook": "",
        "tell_them": "", "aha": "", "cta": "",
        "visual_beats": [], "on_screen_text": [],
        "audio_cues": [], "emotional_arc": "",
        "transcript": transcript.text,
    }


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def render_markdown(analysis: dict[str, Any], video_path: Path) -> str:
    """Render the analysis dict as a human-friendly markdown report."""
    lines: list[str] = []
    lines.append(f"# Video Analysis: {video_path.name}\n")
    lines.append(f"## Summary\n{analysis.get('summary', '')}\n")

    retention_fields = [
        ("Hook (0-2s)", "hook"),
        ("Agitate (2-5s)", "agitate"),
        ("Re-Hook", "re_hook"),
        ("Tell Them", "tell_them"),
        ("Aha Moment", "aha"),
        ("CTA", "cta"),
    ]
    lines.append("## 5-Step Retention Structure\n")
    for label, key in retention_fields:
        value = analysis.get(key, "") or "(none detected)"
        lines.append(f"**{label}:** {value}\n")

    lines.append("\n## Emotional Arc\n")
    lines.append(analysis.get("emotional_arc", "") or "(none)")

    lines.append("\n\n## Visual Beats\n")
    for b in analysis.get("visual_beats", []) or []:
        t = b.get("t", 0)
        desc = b.get("description", "")
        action = b.get("action", "")
        lines.append(f"- **t={t}s** {desc} // {action}")

    lines.append("\n\n## On-Screen Text\n")
    for t in analysis.get("on_screen_text", []) or []:
        lines.append(f"- **t={t.get('t', 0)}s** {t.get('text', '')}")

    lines.append("\n\n## Audio Cues\n")
    for cue in analysis.get("audio_cues", []) or []:
        lines.append(f"- {cue}")

    lines.append("\n\n## Transcript\n")
    lines.append(analysis.get("transcript", "") or "")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a video: extract frames, transcribe audio, align beats, synthesize with Claude.",
    )
    parser.add_argument("video", type=Path, help="Path to the video file")
    parser.add_argument(
        "--model", default="base",
        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
        help="faster-whisper model size (default: base)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Path for the JSON analysis output. Defaults to <video>.analysis.json next to the video.",
    )
    parser.add_argument(
        "--whisper-api", action="store_true",
        help="Use OpenAI Whisper API instead of local faster-whisper. Requires OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--keep-work", action="store_true",
        help="Keep the intermediate work directory (frames, audio) for debugging.",
    )
    args = parser.parse_args()

    video_path: Path = args.video.resolve()
    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        return 2

    # Provider selection. Installer writes VIDEO_PROVIDER to .env.
    # Default to anthropic for pre-installer backward compat.
    provider = (os.environ.get("VIDEO_PROVIDER") or "anthropic").strip().lower()
    if provider not in ("anthropic", "gemini"):
        print(
            f"ERROR: unknown VIDEO_PROVIDER={provider!r}. Expected 'anthropic' or 'gemini'.",
            file=sys.stderr,
        )
        return 2

    key_var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_var)
    if not api_key:
        print(f"ERROR: {key_var} env var not set.", file=sys.stderr)
        print("Run: npx claude-video-install", file=sys.stderr)
        return 2

    output_path: Path = args.output.resolve() if args.output else video_path.with_suffix(
        video_path.suffix + ".analysis.json"
    )

    # Sanity-check ffmpeg is on PATH.
    if shutil.which(FFMPEG) is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        return 2

    # Work dir holds frames + audio. We clean it up unless --keep-work is set.
    work_dir = Path(tempfile.mkdtemp(prefix="videowatch_"))
    try:
        print(f"[1/4] Extracting audio + frames to {work_dir}", file=sys.stderr)
        audio_path = work_dir / "audio.wav"
        extract_audio(video_path, audio_path)

        frames_dir = work_dir / "frames"
        frames = extract_frames_scene(video_path, frames_dir)
        duration = probe_duration(video_path)
        print(
            f"      extracted {len(frames)} frames, duration={duration:.2f}s",
            file=sys.stderr,
        )

        print(f"[2/4] Transcribing audio (model={args.model}, api={args.whisper_api})", file=sys.stderr)
        if args.whisper_api:
            transcript = transcribe_api(audio_path)
        else:
            transcript = transcribe_local(audio_path, args.model)
        print(
            f"      transcript length={len(transcript.text)} chars, "
            f"{len(transcript.words)} words",
            file=sys.stderr,
        )

        print("[3/4] Aligning beats", file=sys.stderr)
        beats = build_beats(frames, transcript, duration)

        is_long = duration > 90 or len(frames) > 20

        if provider == "gemini":
            print(f"[4/4] Synthesizing with Gemini ({GEMINI_MODEL})", file=sys.stderr)
            try:
                import google.generativeai as genai
            except ImportError:
                print(
                    "ERROR: google-generativeai not installed. "
                    "Run: pip install -r requirements.txt",
                    file=sys.stderr,
                )
                return 2
            genai.configure(api_key=api_key)
            gemini_client = genai.GenerativeModel(GEMINI_MODEL)
            if is_long:
                print("      long video, using chunked synthesis", file=sys.stderr)
                analysis = call_gemini_chunked(beats, transcript, gemini_client)
            else:
                analysis = call_gemini_single(beats, transcript, gemini_client)
        else:
            print(f"[4/4] Synthesizing with Claude ({MODEL})", file=sys.stderr)
            try:
                import anthropic
            except ImportError:
                print(
                    "ERROR: anthropic not installed. "
                    "Run: pip install -r requirements.txt",
                    file=sys.stderr,
                )
                return 2
            client = anthropic.Anthropic(api_key=api_key)
            if is_long:
                print("      long video, using chunked synthesis", file=sys.stderr)
                analysis = call_claude_chunked(beats, transcript, client)
            else:
                analysis = call_claude_single(beats, transcript, client)

        # Write JSON next to the video, print markdown report to stdout.
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"      JSON analysis written to {output_path}", file=sys.stderr)

        print(render_markdown(analysis, video_path))
        return 0

    finally:
        if args.keep_work:
            print(f"      work dir kept at {work_dir}", file=sys.stderr)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
