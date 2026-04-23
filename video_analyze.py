"""
video_analyze.py  (V0.2)

Dual-provider pipeline. Two execution paths depending on VIDEO_PROVIDER.

PATH A. Gemini (native multimodal):
    Skip ffmpeg / Whisper / frame extraction entirely. Upload raw .mp4 via
    the google-genai File API, poll until ACTIVE, pass the file directly
    to gemini-2.5-flash/pro alongside the analysis prompt. Gemini hears
    non-speech audio (music, SFX, silence) and sees native framerate.

PATH B. Anthropic (ffmpeg + OCR-augmented):
    Anthropic has no native video input yet, so keep the hack but upgrade it:
    Stage 1. ffmpeg extracts 16kHz mono WAV + scene-change JPEGs.
    Stage 2. faster-whisper (local int8) or OpenAI Whisper API transcribes
             with word-level timestamps.
    Stage 3. easyocr reads on-screen text from each frame up-front, so
             Sonnet does not hallucinate text from a downscaled JPEG.
    Stage 4. Zip frame + speech-window + OCR into beats. Send to
             claude-sonnet-4-6 (faster + cheaper than Opus for vision).
             Long videos chunked into 60s windows + final meta-pass. Stable
             system prompt cached via cache_control.

Output: structured JSON + pretty markdown to stdout.

Usage:
    python video_analyze.py <video> [--model base|small|medium|large]
                                    [--output path]
                                    [--whisper-api]
                                    [--no-ocr]
                                    [--gemini-legacy]
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import platform as _platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 2
ENV_PATH = Path(__file__).resolve().parent / ".env"
_ENV_LOADED = False
try:
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
        _ENV_LOADED = True
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Stage 1: ffmpeg extraction
# --------------------------------------------------------------------------- #

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

SCENE_THRESHOLD_DEFAULT = 0.4
MIN_SCENE_FRAMES = 3
FALLBACK_INTERVAL_SEC = 2.0
FRAME_LONGEST_EDGE = 1024


def run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def probe_has_audio(video_path: Path) -> bool:
    r = run([FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(video_path)])
    return bool((r.stdout or "").strip())


def probe_has_video(video_path: Path) -> bool:
    r = run([FFPROBE, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(video_path)])
    return bool((r.stdout or "").strip())


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
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\n{result.stderr or ''}")


_SCALE_VF = (
    f"scale='if(gt(iw,ih),{FRAME_LONGEST_EDGE},-2):"
    f"if(gt(iw,ih),-2,{FRAME_LONGEST_EDGE})'"
)


def extract_frames_scene(
    video_path: Path,
    frames_dir: Path,
    scene_threshold: float = SCENE_THRESHOLD_DEFAULT,
) -> list[tuple[int, float, Path]]:
    frames_dir.mkdir(parents=True, exist_ok=True)

    temp_pattern = frames_dir / "raw_%04d.jpg"
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vf", f"select='gt(scene,{scene_threshold})',{_SCALE_VF},showinfo",
        "-vsync", "vfr",
        "-q:v", "5",
        str(temp_pattern),
    ]
    result = run(cmd)

    stderr = result.stderr or ""
    ts_pattern = re.compile(r"pts_time:([\d.]+)")
    timestamps = [float(m.group(1)) for m in ts_pattern.finditer(stderr)]

    raw_frames = sorted(frames_dir.glob("raw_*.jpg"))

    if len(raw_frames) < MIN_SCENE_FRAMES:
        for f in raw_frames:
            f.unlink()
        return extract_frames_interval(video_path, frames_dir)

    if len(raw_frames) != len(timestamps):
        print(
            f"      warning: frame/timestamp mismatch "
            f"({len(raw_frames)} frames vs {len(timestamps)} ts) — falling back to interval",
            file=sys.stderr,
        )
        for f in raw_frames:
            f.unlink()
        return extract_frames_interval(video_path, frames_dir)

    results: list[tuple[int, float, Path]] = []
    for i, (raw, ts) in enumerate(zip(raw_frames, timestamps), start=1):
        final_name = f"frame_{i:04d}_t={ts:.2f}.jpg"
        final_path = frames_dir / final_name
        raw.rename(final_path)
        results.append((i, ts, final_path))
    return results


def extract_frames_interval(video_path: Path, frames_dir: Path) -> list[tuple[int, float, Path]]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    temp_pattern = frames_dir / "int_%04d.jpg"
    fps = 1.0 / FALLBACK_INTERVAL_SEC
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vf", f"fps={fps},{_SCALE_VF}",
        "-q:v", "5",
        str(temp_pattern),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed:\n{result.stderr or ''}")

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


def transcribe_local(
    audio_path: Path,
    model_size: str,
    language: str | None = None,
    vad_filter: bool = True,
) -> tuple[Transcript, str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -r requirements.txt"
        ) from e

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        vad_filter=vad_filter,
        language=language,
    )

    words: list[Word] = []
    text_parts: list[str] = []
    for seg in segments:
        text_parts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                words.append(Word(word=w.word.strip(), start=w.start, end=w.end))
    detected = getattr(info, "language", None) or (language or "unknown")
    return Transcript(text=" ".join(text_parts).strip(), words=words), detected


def transcribe_api(audio_path: Path) -> tuple[Transcript, str]:
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
    lang = getattr(response, "language", None) or "unknown"
    return Transcript(text=text.strip(), words=words), lang


# --------------------------------------------------------------------------- #
# Stage 3: timestamp alignment
# --------------------------------------------------------------------------- #

@dataclass
class Beat:
    """One extracted frame plus the speech spoken in its window.

    A beat is the atomic unit we hand to Claude. Each one represents a
    moment in the video where we have both a visual and (possibly) some
    audio context. The synthesis prompt iterates over beats in order.

    V0.2: ocr_text is populated up-front by easyocr so the model reads
    the authoritative text, not a guess from a downscaled JPEG.
    """
    t: float
    frame_path: Path
    speech: str
    ocr_text: str = ""


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

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FILE_API_TIMEOUT_SEC = int(os.environ.get("GEMINI_FILE_API_TIMEOUT_SEC", "900"))

SYSTEM_PROMPT = """You are analyzing a short-form video for a creator who wants to REPLICATE what works. For each beat you are given three things: a frame image, a Speech line (the words spoken in that window), and an OCR line (on-screen text pre-extracted by easyocr).

OCR rules: when the OCR line contains text, copy it VERBATIM into the on_screen_text array — do not reread the frame for text, the downscaled JPEG is unreliable. If OCR is "(no on-screen text detected)" trust that and only report text visible in structural UI (logos, brand marks).

Use the alignment. When a hook lands, point to the exact timestamp and quote the words. When a retention mechanic is at play, name it (open-loop, pattern-interrupt, pay-off, contrast, escalation, social-proof, numeric-claim, contrarian-claim, tool-switch, callback).

For each output field, the creator should be able to screenshot your answer and know what to do differently in the next video. Do NOT describe what the model "sees" — extract the technique.

Return JSON matching this schema exactly:

{
  "schema_version": 2,
  "summary": "2 sentences. No fluff.",
  "hook": {
    "quote": "verbatim words in first 0-2s",
    "timestamp_seconds": 0.0,
    "technique": "named technique",
    "why_it_works": "one sentence, concrete"
  },
  "re_hook": {
    "timestamp_seconds": 0.0,
    "technique": "named pattern-interrupt / reset mechanic",
    "what_would_happen_without_it": "one sentence naming the drop-off mechanism"
  },
  "agitate": { "quote": "...", "timestamp_seconds": 0.0 },
  "aha_moment": { "quote": "...", "timestamp_seconds": 0.0, "setup": "what set it up" },
  "cta": {
    "type": "explicit | implicit | none",
    "quote": "verbatim or visual description",
    "timestamp_seconds": 0.0
  },
  "emotional_arc": [
    { "beat_index": 1, "tone": "curious | urgent | calm | ...", "shift_from_previous": "..." }
  ],
  "retention_mechanics": [
    { "timestamp_seconds": 0.0, "mechanic": "named pattern", "evidence": "quoted speech or named visual change" }
  ],
  "visual_beats": [
    { "timestamp_seconds": 0.0, "frame_description": "...", "unique_signal": "what differentiates this beat from the previous" }
  ],
  "on_screen_text": [
    { "timestamp_seconds": 0.0, "text": "...", "role": "title | caption | callout | brand | cta | numeric_proof" }
  ],
  "audio_cues": [
    { "timestamp_seconds": 0.0, "cue": "...", "role": "music_start | music_swell | sfx | silence | breath" }
  ],
  "replication_checklist": [
    "3-7 concrete items a creator could copy into their next video"
  ],
  "transcript": "full plain transcript"
}

Rules:
- "implicit" CTAs count: "go build it", directional glance toward caption, a visual that IS the pitch.
- unique_signal CANNOT be "same creator talking". If the only signal is repetition, say so.
- Prefer the OCR line over guessing from the frame. Only fall back to frame reading when OCR is empty and text is clearly structural (a wordmark, a big brand lockup).
- If a field is absent, return null (object fields) or an empty array. Never fabricate.
- No emojis. No em dashes.
- Return ONLY valid JSON. No preamble, no markdown code fences."""


CHUNK_SYSTEM_PROMPT = """You are analyzing ONE window of a longer short-form video. Beat-aligned: each beat gives you a frame, a Speech line (words spoken in the window), and an OCR line (on-screen text pre-extracted by easyocr — use this verbatim, do not reread the frame for text).

Extract replication-level detail. Name techniques, don't describe them.

Return JSON:

{
  "window_start": 0.0,
  "window_end": 60.0,
  "segment_summary": "one sentence",
  "visual_beats": [
    { "timestamp_seconds": 0.0, "frame_description": "...", "unique_signal": "..." }
  ],
  "on_screen_text": [
    { "timestamp_seconds": 0.0, "text": "...", "role": "title|caption|callout|brand|cta|numeric_proof" }
  ],
  "retention_mechanics": [
    { "timestamp_seconds": 0.0, "mechanic": "...", "evidence": "..." }
  ],
  "speech": "the words spoken in this window",
  "emotional_register": "one-word tone tag"
}

Rules: copy on-screen text from the OCR line verbatim. No emojis, no em dashes. JSON only."""


META_SYSTEM_PROMPT = """You are combining per-window summaries of a long video into ONE final structured analysis.

You receive an ordered list of window summaries (each already contains visual_beats, on_screen_text, retention_mechanics arrays) plus the full transcript.

Your job is the narrative fields only: summary, hook, re_hook, agitate, aha_moment, cta, emotional_arc, replication_checklist. Python code will concatenate the arrays — do NOT re-emit visual_beats, on_screen_text, retention_mechanics.

Return JSON:

{
  "schema_version": 2,
  "summary": "2 sentences",
  "hook": { "quote": "...", "timestamp_seconds": 0.0, "technique": "...", "why_it_works": "..." },
  "re_hook": { "timestamp_seconds": 0.0, "technique": "...", "what_would_happen_without_it": "..." },
  "agitate": { "quote": "...", "timestamp_seconds": 0.0 },
  "aha_moment": { "quote": "...", "timestamp_seconds": 0.0, "setup": "..." },
  "cta": { "type": "explicit|implicit|none", "quote": "...", "timestamp_seconds": 0.0 },
  "emotional_arc": [ { "beat_index": 1, "tone": "...", "shift_from_previous": "..." } ],
  "replication_checklist": [ "3-7 concrete items" ]
}

Rules: null absent fields, no emojis, no em dashes. JSON only."""


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #

def _is_retryable(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in {
        "APIStatusError", "APIConnectionError", "APITimeoutError",
        "RateLimitError", "InternalServerError", "ServiceUnavailableError",
        "ResourceExhausted", "ServiceUnavailable", "DeadlineExceeded",
        "TooManyRequests", "ServerError", "ReadTimeout", "ConnectionError",
    }:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (408, 429, 500, 502, 503, 504):
        return True
    return False


def retry_api(max_attempts: int = 3, base_delay: float = 2.0) -> Callable:
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*a, **kw):
            last = None
            for attempt in range(max_attempts):
                try:
                    return fn(*a, **kw)
                except Exception as e:
                    last = e
                    if attempt == max_attempts - 1 or not _is_retryable(e):
                        raise
                    delay = base_delay * (4 ** attempt) + random.uniform(0, 1)
                    print(
                        f"      provider call retry {attempt + 1}/{max_attempts - 1} "
                        f"after {e.__class__.__name__}: sleeping {delay:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
            if last:
                raise last
        return wrapped
    return deco


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


# --------------------------------------------------------------------------- #
# OCR (V0.2) — pre-extract on-screen text before handing frames to the model
# --------------------------------------------------------------------------- #

_OCR_READER = None


def _get_ocr_reader():
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER
    try:
        import easyocr  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "easyocr not installed. Run: pip install -r requirements.txt"
        ) from e
    _OCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _OCR_READER


def ocr_frames(
    frames: list[tuple[int, float, Path]],
    min_confidence: float = 0.4,
) -> dict[str, str]:
    """Run easyocr over every extracted frame. Returns {str(frame_path): joined_text}.

    Low-confidence detections are dropped. Multiple detections on the same
    frame are joined with " | " so the model sees them in spatial order.
    """
    reader = _get_ocr_reader()
    out: dict[str, str] = {}
    for _idx, _ts, path in frames:
        try:
            results = reader.readtext(str(path), detail=1, paragraph=False)
        except Exception as e:
            print(f"      OCR failed on {path.name}: {e}", file=sys.stderr)
            out[str(path)] = ""
            continue
        texts: list[str] = []
        for item in results:
            if len(item) >= 3:
                _, text, conf = item[0], item[1], item[2]
                if conf is None or conf >= min_confidence:
                    if text and text.strip():
                        texts.append(text.strip())
            elif len(item) == 2 and item[1]:
                texts.append(str(item[1]).strip())
        out[str(path)] = " | ".join(texts).strip()
    return out


def attach_ocr_to_beats(beats: list[Beat], ocr_map: dict[str, str]) -> None:
    for b in beats:
        b.ocr_text = ocr_map.get(str(b.frame_path), "")


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
        ocr_line = beat.ocr_text if beat.ocr_text else "(no on-screen text detected)"
        blocks.append({"type": "text", "text": f"OCR: {ocr_line}"})
    return blocks


@retry_api()
def _claude_create(anthropic_client, **kw):
    return anthropic_client.messages.create(timeout=120, **kw)


def call_claude_single(
    beats: list[Beat],
    transcript: Transcript,
    anthropic_client,
) -> dict[str, Any]:
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

    response = _claude_create(
        anthropic_client,
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
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": f"Window: {start:.2f}s to {end:.2f}s",
        }]
        content.extend(build_beat_blocks(window_beats))
        content.append({"type": "text", "text": "Return the window JSON now."})

        try:
            response = _claude_create(
                anthropic_client,
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
        except Exception as e:
            print(f"      window {start:.0f}-{end:.0f}s failed: {e}", file=sys.stderr)
            window_json = None

        if window_json is None:
            window_json = {
                "window_start": start,
                "window_end": end,
                "segment_summary": "(parse failed)",
                "visual_beats": [],
                "on_screen_text": [],
                "retention_mechanics": [],
                "speech": " ".join(b.speech for b in window_beats),
                "emotional_register": "unknown",
            }
        window_summaries.append(window_json)

    meta_content = [{
        "type": "text",
        "text": (
            "Full transcript:\n" + transcript.text +
            "\n\nWindow summaries (ordered):\n" +
            json.dumps(window_summaries, indent=2) +
            "\n\nReturn the final narrative JSON now (no arrays)."
        ),
    }]
    try:
        meta_response = _claude_create(
            anthropic_client,
            model=MODEL,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": META_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": meta_content}],
        )
        narrative = parse_response(meta_response, transcript)
    except Exception as e:
        print(f"      meta-pass failed, assembling from windows only: {e}", file=sys.stderr)
        narrative = _empty_narrative(transcript)

    return _merge_windows_narrative(window_summaries, narrative, transcript)


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
    try:
        import PIL.Image
    except ImportError as e:
        raise RuntimeError("Pillow is required. Run: pip install -r requirements.txt") from e

    parts: list[Any] = []
    for i, beat in enumerate(beats, start=1):
        parts.append(f"--- Beat {i} at t={beat.t:.2f}s ---")
        with PIL.Image.open(beat.frame_path) as img:
            img.load()
            parts.append(img.copy())
        speech_line = beat.speech if beat.speech else "(no speech in this window)"
        parts.append(f"Speech: {speech_line}")
        ocr_line = beat.ocr_text if beat.ocr_text else "(no on-screen text detected)"
        parts.append(f"OCR: {ocr_line}")
    return parts


@retry_api()
def _gemini_call(client, *, model: str, contents, system: str, max_output_tokens: int):
    from google.genai import types
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        ),
    )


def call_gemini_single(
    beats: list[Beat],
    transcript: Transcript,
    gemini_client,
) -> dict[str, Any]:
    content: list[Any] = [
        f"Full transcript (for reference):\n{transcript.text}\n\nBeats follow:",
    ]
    content.extend(_gemini_beat_parts(beats))
    content.append("Return the JSON analysis now.")

    response = _gemini_call(
        gemini_client,
        model=GEMINI_MODEL,
        contents=content,
        system=SYSTEM_PROMPT,
        max_output_tokens=16000,
    )
    return parse_gemini_response(response, transcript)


def call_gemini_chunked(
    beats: list[Beat],
    transcript: Transcript,
    gemini_client,
) -> dict[str, Any]:
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
        content: list[Any] = [f"Window: {start:.2f}s to {end:.2f}s"]
        content.extend(_gemini_beat_parts(window_beats))
        content.append("Return the window JSON now.")

        try:
            response = _gemini_call(
                gemini_client,
                model=GEMINI_MODEL,
                contents=content,
                system=CHUNK_SYSTEM_PROMPT,
                max_output_tokens=4000,
            )
            window_json = extract_json(gemini_response_text(response))
        except Exception as e:
            print(f"      window {start:.0f}-{end:.0f}s failed: {e}", file=sys.stderr)
            window_json = None

        if window_json is None:
            window_json = {
                "window_start": start,
                "window_end": end,
                "segment_summary": "(parse failed)",
                "visual_beats": [],
                "on_screen_text": [],
                "retention_mechanics": [],
                "speech": " ".join(b.speech for b in window_beats),
                "emotional_register": "unknown",
            }
        window_summaries.append(window_json)

    meta_content = [
        "Full transcript:\n" + transcript.text +
        "\n\nWindow summaries (ordered):\n" +
        json.dumps(window_summaries, indent=2) +
        "\n\nReturn the final narrative JSON now (no arrays).",
    ]
    try:
        meta_response = _gemini_call(
            gemini_client,
            model=GEMINI_MODEL,
            contents=meta_content,
            system=META_SYSTEM_PROMPT,
            max_output_tokens=8000,
        )
        narrative = parse_gemini_response(meta_response, transcript)
    except Exception as e:
        print(f"      meta-pass failed, assembling from windows only: {e}", file=sys.stderr)
        narrative = _empty_narrative(transcript)

    return _merge_windows_narrative(window_summaries, narrative, transcript)


# --------------------------------------------------------------------------- #
# Gemini native File API path (V0.2) — skip ffmpeg/Whisper entirely
# --------------------------------------------------------------------------- #

@retry_api()
def _gemini_native_generate(client, *, model: str, contents, system: str, max_output_tokens: int):
    from google.genai import types
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        ),
    )


def call_gemini_native_file(
    video_path: Path,
    gemini_client,
) -> dict[str, Any]:
    """Upload the raw video to Gemini's File API and let Gemini read it natively.

    Gemini 2.5 supports video files directly: it sees native framerate and
    hears the full audio stream (music + SFX + silence + speech), so we
    skip our ffmpeg/Whisper hack entirely. No beats, no frame extraction.
    """
    print(
        f"      uploading {video_path.name} to Gemini File API "
        f"({video_path.stat().st_size / (1024*1024):.1f} MB)",
        file=sys.stderr,
    )
    uploaded = gemini_client.files.upload(file=str(video_path))

    deadline = time.time() + GEMINI_FILE_API_TIMEOUT_SEC
    while True:
        info = gemini_client.files.get(name=uploaded.name)
        state = getattr(info, "state", None)
        state_name = getattr(state, "name", None) or str(state or "")
        if state_name == "ACTIVE":
            uploaded = info
            break
        if state_name == "FAILED":
            raise RuntimeError(
                f"Gemini File API reported FAILED processing {video_path.name}"
            )
        if time.time() > deadline:
            raise RuntimeError(
                f"Gemini File API processing timed out after "
                f"{GEMINI_FILE_API_TIMEOUT_SEC}s for {video_path.name}"
            )
        time.sleep(2.0)

    print(f"      file ACTIVE, calling {GEMINI_MODEL} with native video", file=sys.stderr)

    user_prompt = (
        "Watch this video end-to-end. You have native access to every frame "
        "and the full audio stream (speech, music, SFX, silence). Extract the "
        "retention structure per the system schema. Include on-screen text "
        "verbatim (you have native OCR), and populate audio_cues for music "
        "swells, silence beats, and SFX that the speech transcript cannot "
        "capture. Return the JSON analysis now."
    )
    contents = [uploaded, user_prompt]

    transcript_placeholder = Transcript(text="", words=[])
    try:
        response = _gemini_native_generate(
            gemini_client,
            model=GEMINI_MODEL,
            contents=contents,
            system=SYSTEM_PROMPT,
            max_output_tokens=16000,
        )
        return parse_gemini_response(response, transcript_placeholder)
    finally:
        try:
            gemini_client.files.delete(name=uploaded.name)
        except Exception as e:
            print(
                f"      warning: could not delete uploaded Gemini file "
                f"{uploaded.name}: {e}",
                file=sys.stderr,
            )


def _empty_narrative(transcript: Transcript) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": "(synthesis failed, assembled from window data only)",
        "hook": None, "re_hook": None, "agitate": None,
        "aha_moment": None, "cta": None,
        "emotional_arc": [],
        "replication_checklist": [],
        "transcript": transcript.text,
    }


def _merge_windows_narrative(
    windows: list[dict[str, Any]],
    narrative: dict[str, Any],
    transcript: Transcript,
) -> dict[str, Any]:
    visual_beats: list[Any] = []
    on_screen_text: list[Any] = []
    retention_mechanics: list[Any] = []
    audio_cues: list[Any] = []
    for w in windows:
        visual_beats.extend(w.get("visual_beats") or [])
        on_screen_text.extend(w.get("on_screen_text") or [])
        retention_mechanics.extend(w.get("retention_mechanics") or [])
        audio_cues.extend(w.get("audio_cues") or [])

    def _ts(item):
        if isinstance(item, dict):
            return item.get("timestamp_seconds") or item.get("t") or 0.0
        return 0.0

    visual_beats.sort(key=_ts)
    on_screen_text.sort(key=_ts)
    retention_mechanics.sort(key=_ts)

    out = dict(narrative)
    out["schema_version"] = SCHEMA_VERSION
    out["visual_beats"] = visual_beats
    out["on_screen_text"] = on_screen_text
    out["retention_mechanics"] = retention_mechanics
    out["audio_cues"] = audio_cues
    if not out.get("transcript"):
        out["transcript"] = transcript.text
    return out


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
    text = gemini_response_text(response)
    data = extract_json(text)
    if data is None:
        return _failed_analysis(transcript, text)
    data.setdefault("schema_version", SCHEMA_VERSION)
    if not data.get("transcript"):
        data["transcript"] = transcript.text
    return data


def extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break

    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _failed_analysis(transcript: Transcript, raw: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": "(synthesis failed to return parseable JSON)",
        "hook": None, "re_hook": None, "agitate": None,
        "aha_moment": None, "cta": None,
        "emotional_arc": [],
        "retention_mechanics": [],
        "visual_beats": [], "on_screen_text": [],
        "audio_cues": [],
        "replication_checklist": [],
        "transcript": transcript.text,
        "raw_response": raw,
    }


def parse_response(response, transcript: Transcript) -> dict[str, Any]:
    text = response_text(response)
    data = extract_json(text)
    if data is None:
        return _failed_analysis(transcript, text)
    data.setdefault("schema_version", SCHEMA_VERSION)
    if not data.get("transcript"):
        data["transcript"] = transcript.text
    return data


def empty_analysis(transcript: Transcript) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": "(no beats extracted)",
        "hook": None, "re_hook": None, "agitate": None,
        "aha_moment": None, "cta": None,
        "emotional_arc": [],
        "retention_mechanics": [],
        "visual_beats": [], "on_screen_text": [],
        "audio_cues": [],
        "replication_checklist": [],
        "transcript": transcript.text,
    }


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def _fmt_ts(x: Any) -> str:
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_beat_field(val: Any, fields: list[str]) -> str:
    if val is None:
        return "(none detected)"
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        parts = []
        if "quote" in val and val.get("quote"):
            parts.append(f'"{val["quote"]}"')
        ts = val.get("timestamp_seconds")
        if ts is not None:
            parts.append(f"@ {_fmt_ts(ts)}s")
        for k in fields:
            if k in val and val.get(k):
                parts.append(f"[{k}: {val[k]}]")
        return " ".join(parts) if parts else "(none detected)"
    return str(val)


def render_markdown(analysis: dict[str, Any], video_path: Path, detected_lang: str | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# Video Analysis: {video_path.name}\n")
    if detected_lang:
        lines.append(f"_detected language: {detected_lang}_\n")
    lines.append(f"## Summary\n{analysis.get('summary', '')}\n")

    lines.append("## Retention Structure\n")
    lines.append(f"**Hook:** {_fmt_beat_field(analysis.get('hook'), ['technique', 'why_it_works'])}\n")
    lines.append(f"**Re-Hook:** {_fmt_beat_field(analysis.get('re_hook'), ['technique', 'what_would_happen_without_it'])}\n")
    lines.append(f"**Agitate:** {_fmt_beat_field(analysis.get('agitate'), [])}\n")
    lines.append(f"**Aha Moment:** {_fmt_beat_field(analysis.get('aha_moment') or analysis.get('aha'), ['setup'])}\n")
    lines.append(f"**CTA:** {_fmt_beat_field(analysis.get('cta'), ['type'])}\n")

    mechanics = analysis.get("retention_mechanics") or []
    if mechanics:
        lines.append("\n## Retention Mechanics\n")
        for m in mechanics:
            if isinstance(m, dict):
                lines.append(
                    f"- **t={_fmt_ts(m.get('timestamp_seconds'))}s** "
                    f"`{m.get('mechanic', '')}` — {m.get('evidence', '')}"
                )
            else:
                lines.append(f"- {m}")

    checklist = analysis.get("replication_checklist") or []
    if checklist:
        lines.append("\n## Replication Checklist\n")
        for item in checklist:
            lines.append(f"- [ ] {item}")

    arc = analysis.get("emotional_arc")
    lines.append("\n## Emotional Arc\n")
    if isinstance(arc, list) and arc:
        for entry in arc:
            if isinstance(entry, dict):
                lines.append(
                    f"- beat {entry.get('beat_index', '?')}: "
                    f"{entry.get('tone', '')} — {entry.get('shift_from_previous', '')}"
                )
            else:
                lines.append(f"- {entry}")
    elif isinstance(arc, str) and arc:
        lines.append(arc)
    else:
        lines.append("(none)")

    lines.append("\n## Visual Beats\n")
    for b in analysis.get("visual_beats", []) or []:
        if isinstance(b, dict):
            ts = _fmt_ts(b.get("timestamp_seconds", b.get("t", 0)))
            desc = b.get("frame_description", b.get("description", ""))
            sig = b.get("unique_signal", b.get("action", ""))
            lines.append(f"- **t={ts}s** {desc}  ·  _{sig}_")

    lines.append("\n## On-Screen Text\n")
    for t in analysis.get("on_screen_text", []) or []:
        if isinstance(t, dict):
            ts = _fmt_ts(t.get("timestamp_seconds", t.get("t", 0)))
            role = t.get("role")
            role_s = f" ({role})" if role else ""
            lines.append(f"- **t={ts}s** {t.get('text', '')}{role_s}")

    cues = analysis.get("audio_cues") or []
    if cues:
        lines.append("\n## Audio Cues\n")
        for cue in cues:
            if isinstance(cue, dict):
                lines.append(f"- **t={_fmt_ts(cue.get('timestamp_seconds'))}s** {cue.get('cue', '')} ({cue.get('role', '')})")
            else:
                lines.append(f"- {cue}")

    raw = analysis.get("raw_response")
    if raw:
        lines.append("\n## Raw Synthesis Response (parse failed)\n")
        lines.append("```")
        lines.append(str(raw)[:4000])
        lines.append("```")

    lines.append("\n## Transcript\n")
    lines.append(analysis.get("transcript", "") or "")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _ffmpeg_hint() -> str:
    sysname = _platform.system().lower()
    if sysname == "windows":
        return "install: winget install Gyan.FFmpeg"
    if sysname == "darwin":
        return "install: brew install ffmpeg"
    return "install: sudo apt install ffmpeg"


_URL_PREFIXES = ("http://", "https://", "www.", "youtube.com", "youtu.be", "tiktok.com", "instagram.com")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a local short-form video: extract frames, transcribe audio, align beats, synthesize retention breakdown.",
    )
    parser.add_argument("video", type=str, help="Path to the local video file")
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
    parser.add_argument("--lang", default=None, help="Force transcription language (ISO code). Default: autodetect.")
    parser.add_argument("--no-vad", action="store_true", help="Disable whisper VAD filter.")
    parser.add_argument(
        "--scene-threshold", type=float, default=SCENE_THRESHOLD_DEFAULT,
        help=f"ffmpeg scene-change threshold (default: {SCENE_THRESHOLD_DEFAULT})",
    )
    parser.add_argument(
        "--keep-work", action="store_true",
        help="Keep the intermediate work directory (frames, audio) for debugging.",
    )
    parser.add_argument(
        "--no-ocr", action="store_true",
        help="Disable easyocr pre-extraction (Claude path only).",
    )
    parser.add_argument(
        "--gemini-legacy", action="store_true",
        help="Force the Gemini path through the legacy ffmpeg+Whisper+frames hack "
             "instead of the native File API. Useful when File API is unavailable.",
    )
    args = parser.parse_args()

    raw_video = args.video
    lower = raw_video.lower().lstrip()
    if lower.startswith(_URL_PREFIXES):
        print(
            "ERROR: video-watch analyzes local files only. Download the video first and pass the local path.",
            file=sys.stderr,
        )
        return 2

    video_path: Path = Path(raw_video).resolve()
    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        return 2

    if not _ENV_LOADED:
        print(
            f"warning: no .env at {ENV_PATH}; running with shell environment. "
            f"Run 'npx video-watch-install' (or 'npx claude-video-install') to configure.",
            file=sys.stderr,
        )

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
        print("Run: npx video-watch-install", file=sys.stderr)
        return 2

    output_path: Path = args.output.resolve() if args.output else video_path.with_suffix(
        video_path.suffix + ".analysis.json"
    )

    gemini_native = (provider == "gemini") and not args.gemini_legacy

    if not gemini_native:
        if shutil.which(FFMPEG) is None:
            print(f"ERROR: ffmpeg not found on PATH. {_ffmpeg_hint()}", file=sys.stderr)
            return 2
        if shutil.which(FFPROBE) is None:
            print(f"ERROR: ffprobe not found on PATH. {_ffmpeg_hint()}", file=sys.stderr)
            return 2

        # Eager whisper import so we fail fast before ffmpeg runs.
        if not args.whisper_api:
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                print(
                    "ERROR: faster-whisper is not installed. Run: pip install -r requirements.txt",
                    file=sys.stderr,
                )
                return 2

        if not probe_has_video(video_path):
            print(
                f"ERROR: {video_path} contains no video stream. "
                f"video-watch analyzes video content; use a transcription-only tool for audio.",
                file=sys.stderr,
            )
            return 2
        has_audio = probe_has_audio(video_path)
    else:
        has_audio = True  # unused in the Gemini native path

    detected_lang: str | None = None

    # --- Path A: Gemini native File API (no ffmpeg, no Whisper, no frames) ---
    if gemini_native:
        print(f"[1/1] Synthesizing with Gemini native File API ({GEMINI_MODEL})", file=sys.stderr)
        try:
            from google import genai  # type: ignore
        except ImportError:
            print(
                "ERROR: google-genai not installed. "
                "Run: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 2
        gemini_client = genai.Client(api_key=api_key)
        analysis = call_gemini_native_file(video_path, gemini_client)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"      JSON analysis written to {output_path}", file=sys.stderr)

        print(render_markdown(analysis, video_path, detected_lang=None))
        return 0

    # --- Path B: Anthropic (ffmpeg + Whisper + OCR + Sonnet) ---
    work_dir = Path(tempfile.mkdtemp(prefix="videowatch_"))
    try:
        print(f"[1/5] Extracting audio + frames to {work_dir}", file=sys.stderr)
        audio_path = work_dir / "audio.wav"
        if has_audio:
            extract_audio(video_path, audio_path)
        else:
            print("      (no audio track — skipping audio extraction and transcription)", file=sys.stderr)

        frames_dir = work_dir / "frames"
        frames = extract_frames_scene(video_path, frames_dir, scene_threshold=args.scene_threshold)
        duration = probe_duration(video_path)
        print(
            f"      extracted {len(frames)} frames, duration={duration:.2f}s",
            file=sys.stderr,
        )

        if has_audio:
            print(f"[2/5] Transcribing audio (model={args.model}, api={args.whisper_api})", file=sys.stderr)
            if args.whisper_api:
                transcript, detected_lang = transcribe_api(audio_path)
            else:
                transcript, detected_lang = transcribe_local(
                    audio_path, args.model,
                    language=args.lang,
                    vad_filter=not args.no_vad,
                )
            print(
                f"      transcript length={len(transcript.text)} chars, "
                f"{len(transcript.words)} words, detected_language={detected_lang}",
                file=sys.stderr,
            )
        else:
            transcript = Transcript(text="", words=[])
            print("[2/5] (skipped — no audio)", file=sys.stderr)

        print("[3/5] Aligning beats", file=sys.stderr)
        beats = build_beats(frames, transcript, duration)

        if args.no_ocr:
            print("[4/5] OCR skipped (--no-ocr)", file=sys.stderr)
        else:
            print(f"[4/5] Running OCR over {len(frames)} frames (easyocr)", file=sys.stderr)
            try:
                ocr_map = ocr_frames(frames)
                attach_ocr_to_beats(beats, ocr_map)
                hits = sum(1 for b in beats if b.ocr_text)
                print(
                    f"      OCR produced text on {hits}/{len(beats)} beats",
                    file=sys.stderr,
                )
            except Exception as e:
                print(
                    f"      OCR failed ({e}); continuing without on-screen-text hints",
                    file=sys.stderr,
                )

        is_long = duration > 90 or len(frames) > 20

        if provider == "gemini":
            # Legacy Gemini path: same ffmpeg+Whisper+beats pipeline as Claude,
            # only reached when --gemini-legacy is passed.
            print(f"[5/5] Synthesizing with Gemini (legacy, {GEMINI_MODEL})", file=sys.stderr)
            try:
                from google import genai  # type: ignore
            except ImportError:
                print(
                    "ERROR: google-genai not installed. "
                    "Run: pip install -r requirements.txt",
                    file=sys.stderr,
                )
                return 2
            gemini_client = genai.Client(api_key=api_key)
            if is_long:
                print("      long video, using chunked synthesis", file=sys.stderr)
                analysis = call_gemini_chunked(beats, transcript, gemini_client)
            else:
                analysis = call_gemini_single(beats, transcript, gemini_client)
        else:
            print(f"[5/5] Synthesizing with Claude ({MODEL})", file=sys.stderr)
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

        if detected_lang:
            analysis["detected_language"] = detected_lang

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"      JSON analysis written to {output_path}", file=sys.stderr)

        print(render_markdown(analysis, video_path, detected_lang=detected_lang))
        return 0

    finally:
        if args.keep_work:
            print(f"      work dir kept at {work_dir}", file=sys.stderr)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
