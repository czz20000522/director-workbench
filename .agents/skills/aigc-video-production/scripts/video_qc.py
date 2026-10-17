#!/usr/bin/env python3
"""Extract review frames and lightweight audio/video metrics from a rendered shot."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def sample_frame_indices(frame_count: int, sample_count: int = 5) -> list[int]:
    if frame_count <= 0 or sample_count <= 0:
        return []
    if frame_count <= sample_count:
        return list(range(frame_count))
    return sorted({round(index * (frame_count - 1) / (sample_count - 1)) for index in range(sample_count)})


def audio_peak_status(peak: float) -> str:
    if peak >= 0.99:
        return "clipping_risk"
    if peak >= 0.9:
        return "hot"
    return "pass"


def _count_frames(video_path: Path) -> int:
    import av

    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        if stream.frames:
            return int(stream.frames)
        return sum(1 for _ in container.decode(stream))


def _audio_metrics(video_path: Path) -> dict[str, Any]:
    import av
    import numpy as np

    with av.open(str(video_path)) as container:
        if not container.streams.audio:
            return {"present": False}
        stream = container.streams.audio[0]
        total = 0
        sum_value = 0.0
        sum_square = 0.0
        peak = 0.0
        for frame in container.decode(stream):
            raw = frame.to_ndarray()
            values = raw.astype(np.float64)
            if np.issubdtype(raw.dtype, np.integer):
                values /= max(abs(np.iinfo(raw.dtype).min), np.iinfo(raw.dtype).max)
            flat = values.ravel()
            if not flat.size:
                continue
            total += int(flat.size)
            sum_value += float(flat.sum())
            sum_square += float(np.square(flat).sum())
            peak = max(peak, float(np.abs(flat).max()))
        if not total:
            return {"present": True, "codec": stream.codec_context.name, "samples": 0}
        mean = sum_value / total
        rms = math.sqrt(sum_square / total)
        centered_rms = math.sqrt(max(0.0, sum_square / total - mean * mean))
        return {
            "present": True,
            "codec": stream.codec_context.name,
            "channels": stream.codec_context.channels,
            "sample_rate": stream.codec_context.sample_rate,
            "samples": total,
            "mean": round(mean, 7),
            "rms": round(rms, 7),
            "centered_rms": round(centered_rms, 7),
            "peak": round(peak, 7),
            "peak_status": audio_peak_status(peak),
        }


def analyze_video(video_path: str | Path, output_dir: str | Path, sample_count: int = 5) -> dict[str, Any]:
    import av
    from PIL import Image

    source = Path(video_path).resolve()
    destination = Path(output_dir).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.mkdir(parents=True, exist_ok=True)

    frame_count = _count_frames(source)
    targets = set(sample_frame_indices(frame_count, sample_count))
    frames: list[tuple[int, Path]] = []
    video_info: dict[str, Any]
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate) if stream.average_rate else None
        duration = float(stream.duration * stream.time_base) if stream.duration else None
        video_info = {
            "codec": stream.codec_context.name,
            "width": stream.codec_context.width,
            "height": stream.codec_context.height,
            "fps": round(rate, 4) if rate else None,
            "frame_count": frame_count,
            "duration_seconds": round(duration, 4) if duration else None,
        }
        last_image = None
        for index, frame in enumerate(container.decode(stream)):
            image = frame.to_image().convert("RGB")
            last_image = image
            if index in targets:
                frame_path = destination / f"frame-{index:05d}.png"
                image.save(frame_path)
                frames.append((index, frame_path))
        if last_image is not None:
            last_image.save(destination / "last-frame.png")

    thumbnails = []
    for _, frame_path in frames:
        with Image.open(frame_path) as opened:
            thumbnail = opened.convert("RGB")
            thumbnail.thumbnail((384, 216), Image.Resampling.LANCZOS)
            thumbnails.append(thumbnail.copy())
    if thumbnails:
        columns = min(3, len(thumbnails))
        rows = math.ceil(len(thumbnails) / columns)
        cell_width = max(image.width for image in thumbnails)
        cell_height = max(image.height for image in thumbnails)
        contact = Image.new("RGB", (columns * cell_width, rows * cell_height), (30, 30, 30))
        for index, thumbnail in enumerate(thumbnails):
            x = (index % columns) * cell_width
            y = (index // columns) * cell_height
            contact.paste(thumbnail, (x, y))
        contact.save(destination / "contact-sheet.png")

    report = {
        "source": str(source),
        "size_bytes": source.stat().st_size,
        "video": video_info,
        "audio": _audio_metrics(source),
        "sample_frames": [str(path) for _, path in frames],
        "last_frame": str(destination / "last-frame.png"),
        "contact_sheet": str(destination / "contact-sheet.png") if thumbnails else None,
    }
    (destination / "qc.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Extract AIGC video review artifacts")
    parser.add_argument("video", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    try:
        report = analyze_video(args.video, args.output_dir, args.samples)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
