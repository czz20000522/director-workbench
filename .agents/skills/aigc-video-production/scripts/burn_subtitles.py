#!/usr/bin/env python3
"""Burn an approved ASS track into a finished picture master."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_ffmpeg(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.resolve()
    elif found := shutil.which("ffmpeg"):
        candidate = Path(found).resolve()
    else:
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise RuntimeError("ffmpeg is not on PATH and imageio-ffmpeg is unavailable") from exc
        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def media_info(path: Path) -> dict[str, object]:
    import av

    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f"No video stream: {path}")
        video = container.streams.video[0]
        audio = container.streams.audio[0] if container.streams.audio else None
        return {
            "width": video.codec_context.width,
            "height": video.codec_context.height,
            "fps": float(video.average_rate) if video.average_rate else None,
            "frames": int(video.frames) if video.frames else None,
            "duration": float(video.duration * video.time_base) if video.duration else None,
            "video_codec": video.codec_context.name,
            "audio_codec": audio.codec_context.name if audio else None,
            "audio_rate": audio.codec_context.sample_rate if audio else None,
            "audio_channels": audio.codec_context.channels if audio else None,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("subtitles", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument(
        "--font-file",
        type=Path,
        default=Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    )
    parser.add_argument("--expected-width", type=int)
    parser.add_argument("--expected-height", type=int)
    parser.add_argument("--crf", type=int, default=16)
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()

    source = args.input.resolve()
    subtitles = args.subtitles.resolve()
    output = args.output.resolve()
    font_file = args.font_file.resolve()
    for path in (source, subtitles, font_file):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")

    before = media_info(source)
    if args.expected_width and before["width"] != args.expected_width:
        raise ValueError(f"Expected width {args.expected_width}, got {before['width']}")
    if args.expected_height and before["height"] != args.expected_height:
        raise ValueError(f"Expected height {args.expected_height}, got {before['height']}")
    if before["audio_codec"] is None:
        raise ValueError("The approved master has no audio stream")

    ffmpeg = find_ffmpeg(args.ffmpeg)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".subtitle-", dir=output.parent) as temporary:
        work_dir = Path(temporary)
        shutil.copy2(subtitles, work_dir / "captions.ass")
        fonts_dir = work_dir / "fonts"
        fonts_dir.mkdir()
        shutil.copy2(font_file, fonts_dir / "NotoSansSC-VF.ttf")
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source),
            "-vf",
            "ass=filename=captions.ass:fontsdir=fonts",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            args.preset,
            "-crf",
            str(args.crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        completed = subprocess.run(command, cwd=work_dir, check=False)
    if completed.returncode != 0:
        if output.exists():
            output.unlink()
        raise RuntimeError(f"ffmpeg failed with exit code {completed.returncode}")

    try:
        after = media_info(output)
        for key in (
            "width",
            "height",
            "fps",
            "frames",
            "audio_codec",
            "audio_rate",
            "audio_channels",
        ):
            if before[key] != after[key]:
                raise ValueError(
                    f"Delivery mismatch for {key}: {before[key]} -> {after[key]}"
                )
        if before["duration"] is not None and after["duration"] is not None:
            if abs(float(before["duration"]) - float(after["duration"])) > 1 / 24:
                raise ValueError(
                    f"Duration changed: {before['duration']} -> {after['duration']}"
                )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
