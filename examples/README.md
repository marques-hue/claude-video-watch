# Examples

Drop test videos into this directory.

Suggested files for smoke-testing:

- A short (under 60s) reel or short-form clip with clear spoken audio. Validates the single-call synthesis path.
- A longer (over 90s) clip. Validates the chunked synthesis path with the 60-second windowing and meta-pass.
- A silent / static screen recording. Validates the fallback from scene detection to fixed-interval frame sampling.

This directory is gitignored, so anything you put here stays local.

Run from repo root:

```bash
python video_analyze.py examples/your-clip.mp4
```
