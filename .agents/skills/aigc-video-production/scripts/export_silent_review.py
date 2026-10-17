#!/usr/bin/env python3
"""Export a picture-only review file without changing generated video frames."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from burn_subtitles import find_ffmpeg, media_info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    args = parser.parse_args()

    source = args.input.resolve()
    output = args.output.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")

    before = media_info(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(find_ffmpeg(args.ffmpeg)),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-movflags",
        "+faststart",
        str(output),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        if output.exists():
            output.unlink()
        raise RuntimeError(f"ffmpeg failed with exit code {completed.returncode}")

    try:
        after = media_info(output)
        for key in ("width", "height", "fps", "frames", "video_codec"):
            if before[key] != after[key]:
                raise ValueError(
                    f"Silent review mismatch for {key}: {before[key]} -> {after[key]}"
                )
        if before["duration"] is not None and after["duration"] is not None:
            if abs(float(before["duration"]) - float(after["duration"])) > 1 / 24:
                raise ValueError(
                    f"Silent review duration changed: {before['duration']} -> {after['duration']}"
                )
        if after["audio_codec"] is not None:
            raise ValueError("Silent review unexpectedly contains an audio stream")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
