"""Smoke test: verifies imports succeed and ffmpeg is callable.

Does not call the Anthropic API. Does not transcribe anything. This is the
absolute minimum "the environment isn't broken" check.

Run: python test_smoke.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def check_ffmpeg() -> bool:
    if shutil.which("ffmpeg") is None:
        print("FAIL: ffmpeg not on PATH")
        return False
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    if result.returncode != 0:
        print("FAIL: ffmpeg returned nonzero")
        return False
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    print(f"OK: ffmpeg -> {first_line}")
    return True


def check_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        print(f"OK: faster_whisper {faster_whisper.__version__ if hasattr(faster_whisper, '__version__') else ''}")
        return True
    except ImportError as e:
        print(f"FAIL: faster-whisper import: {e}")
        return False


def check_anthropic() -> bool:
    try:
        import anthropic
        print(f"OK: anthropic {anthropic.__version__}")
        return True
    except ImportError as e:
        print(f"FAIL: anthropic import: {e}")
        return False


def check_pipeline_module() -> bool:
    try:
        import video_analyze  # noqa: F401
        print("OK: video_analyze imports cleanly")
        return True
    except Exception as e:
        print(f"FAIL: video_analyze import: {e}")
        return False


def main() -> int:
    results = [
        check_ffmpeg(),
        check_faster_whisper(),
        check_anthropic(),
        check_pipeline_module(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
