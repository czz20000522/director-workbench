#!/usr/bin/env python3
"""Concatenate voiced shots into a reviewable story scene."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def build_concat_filter(input_count: int) -> str:
    if input_count < 2:
        raise ValueError("至少需要两个镜头")
    resets: list[str] = []
    links: list[str] = []
    for index in range(input_count):
        resets.append(f"[{index}:v:0]fps=24,setpts=PTS-STARTPTS[v{index}]")
        resets.append(f"[{index}:a:0]asetpts=PTS-STARTPTS[a{index}]")
        links.extend((f"[v{index}]", f"[a{index}]"))
    concat = "".join(links) + f"concat=n={input_count}:v=1:a=1[v][a]"
    return ";".join((*resets, concat))


def build_ffmpeg_command(ffmpeg: str, inputs: list[Path], output: Path) -> list[str]:
    if len(inputs) < 2:
        raise ValueError("至少需要两个镜头")
    command = [ffmpeg, "-y"]
    for source in inputs:
        command.extend(("-i", str(source)))
    command.extend(
        (
            "-filter_complex",
            build_concat_filter(len(inputs)),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        )
    )
    return command


def concat_shots(inputs: list[Path], output: Path) -> Path:
    if len(inputs) < 2:
        raise ValueError("至少需要两个镜头")
    missing = [path for path in inputs if not path.is_file()]
    if missing:
        raise FileNotFoundError(missing[0])

    import imageio_ffmpeg

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(
        imageio_ffmpeg.get_ffmpeg_exe(),
        [source.resolve() for source in inputs],
        output,
    )
    subprocess.run(command, check=True)
    return output


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Concatenate voiced video shots")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        path = concat_shots(args.inputs, args.output)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc))
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
