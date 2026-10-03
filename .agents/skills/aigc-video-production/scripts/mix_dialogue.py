#!/usr/bin/env python3
"""Mix one delayed dialogue line over an H3 video while copying the video stream."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def build_ffmpeg_command(
    ffmpeg: str,
    video: Path,
    dialogue: Path,
    output: Path,
    *,
    dialogue_start: float,
    bed_gain: float,
    dialogue_gain: float,
) -> list[str]:
    if dialogue_start < 0:
        raise ValueError("dialogue_start 不能为负数")
    if bed_gain <= 0 or dialogue_gain <= 0:
        raise ValueError("音量倍率必须大于零")
    delay_ms = round(dialogue_start * 1000)
    filter_graph = (
        f"[0:a]volume={bed_gain}[bed];"
        f"[1:a]volume={dialogue_gain},adelay={delay_ms}:all=1[voice];"
        "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.9[mix]"
    )
    return [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-i",
        str(dialogue),
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[mix]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]


def mix_dialogue(
    video: str | Path,
    dialogue: str | Path,
    output: str | Path,
    *,
    dialogue_start: float,
    bed_gain: float,
    dialogue_gain: float,
) -> dict[str, object]:
    import imageio_ffmpeg

    video_path = Path(video).resolve()
    dialogue_path = Path(dialogue).resolve()
    output_path = Path(output).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(video_path)
    if not dialogue_path.is_file():
        raise FileNotFoundError(dialogue_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(
        imageio_ffmpeg.get_ffmpeg_exe(),
        video_path,
        dialogue_path,
        output_path,
        dialogue_start=dialogue_start,
        bed_gain=bed_gain,
        dialogue_gain=dialogue_gain,
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-4000:])
    report = {
        "video": str(video_path),
        "dialogue": str(dialogue_path),
        "output": str(output_path),
        "dialogue_start": dialogue_start,
        "bed_gain": bed_gain,
        "dialogue_gain": dialogue_gain,
        "size_bytes": output_path.stat().st_size,
    }
    output_path.with_suffix(".mix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Mix one dialogue line into a generated video shot")
    parser.add_argument("video", type=Path)
    parser.add_argument("dialogue", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dialogue-start", type=float, required=True)
    parser.add_argument("--bed-gain", type=float, default=0.5)
    parser.add_argument("--dialogue-gain", type=float, default=1.0)
    args = parser.parse_args()
    try:
        report = mix_dialogue(
            args.video,
            args.dialogue,
            args.output,
            dialogue_start=args.dialogue_start,
            bed_gain=args.bed_gain,
            dialogue_gain=args.dialogue_gain,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
