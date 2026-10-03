"""Verify a planned multi-segment delivery against the files on disk.

This is a media check, not an artistic approval. It deliberately reports
short-tail trims and hot audio as review findings instead of silently passing
them as finished content.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _duration(stream: Any, container: Any) -> float | None:
    if stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    if container.duration:
        return float(container.duration / 1_000_000)
    return None


def inspect_media(path: Path, expected_duration: float | None = None, require_audio: bool = True) -> dict[str, Any]:
    import av
    import numpy as np

    result: dict[str, Any] = {"path": str(path), "exists": path.is_file(), "issues": []}
    if not path.is_file():
        result["issues"].append("missing_file")
        return result

    try:
        with av.open(str(path)) as container:
            video_stream = container.streams.video[0] if container.streams.video else None
            audio_stream = container.streams.audio[0] if container.streams.audio else None
            if video_stream is None:
                result["issues"].append("missing_video_stream")
                return result

            frame_count = 0
            sampled_luma: list[float] = []
            first_frame = None
            last_frame = None
            for frame in container.decode(video_stream):
                if first_frame is None:
                    first_frame = frame
                last_frame = frame
                frame_count += 1
                if frame_count in {1, 2, 3}:
                    sampled_luma.append(float(np.asarray(frame.to_ndarray(format="gray"), dtype=np.float32).mean()))
            if last_frame is not None:
                sampled_luma.append(float(np.asarray(last_frame.to_ndarray(format="gray"), dtype=np.float32).mean()))

            duration = _duration(video_stream, container)
            fps = float(video_stream.average_rate) if video_stream.average_rate else None
            result["video"] = {
                "codec": video_stream.codec_context.name,
                "width": video_stream.codec_context.width,
                "height": video_stream.codec_context.height,
                "fps": round(fps, 4) if fps else None,
                "frame_count": frame_count,
                "duration_seconds": round(duration, 4) if duration is not None else None,
                "sample_luma": [round(value, 3) for value in sampled_luma],
            }
            if duration is None or frame_count == 0:
                result["issues"].append("undecodable_video")
            if any(value <= 1.0 for value in sampled_luma):
                result["issues"].append("sampled_black_frame")
            if expected_duration is not None and duration is not None and abs(duration - expected_duration) > 0.25:
                result["issues"].append("duration_mismatch")

            if audio_stream is None:
                result["audio"] = {"present": False}
                if require_audio:
                    result["issues"].append("missing_audio")
            else:
                total = 0
                peak = 0.0
                # The video decode iterator has already advanced the demuxer;
                # open a second container so audio metrics are not lost.
                with av.open(str(path)) as audio_container:
                    audio_stream = audio_container.streams.audio[0]
                    for frame in audio_container.decode(audio_stream):
                        raw = frame.to_ndarray()
                        values = raw.astype(np.float64)
                        if np.issubdtype(raw.dtype, np.integer):
                            values /= max(abs(np.iinfo(raw.dtype).min), np.iinfo(raw.dtype).max)
                        if values.size:
                            total += int(values.size)
                            peak = max(peak, float(np.abs(values).max()))
                result["audio"] = {
                    "present": True,
                    "codec": audio_stream.codec_context.name,
                    "channels": audio_stream.codec_context.channels,
                    "sample_rate": audio_stream.codec_context.sample_rate,
                    "samples": total,
                    "peak": round(peak, 7),
                    "peak_status": "clipping_risk" if peak >= 0.99 else "hot" if peak >= 0.9 else "pass",
                }
                if require_audio and not total:
                    result["issues"].append("empty_audio")
    except Exception as exc:  # report the file and keep checking the rest
        result["issues"].append("decode_error")
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["ok"] = not result["issues"]
    return result


def verify_delivery(plan_path: Path, shared_root: Path, report_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    segments: list[dict[str, Any]] = []
    for segment in plan.get("segments", []):
        relative = ((segment.get("video") or {}).get("path"))
        path = shared_root / relative if relative and relative.replace("\\", "/").startswith("output/") else shared_root / "output" / relative if relative else Path("")
        inspected = inspect_media(path, float(segment.get("duration_seconds")) if segment.get("status") != "video_pending_trim" else None, require_audio=False)
        inspected["id"] = segment.get("id")
        inspected["planned_status"] = segment.get("status")
        segments.append(inspected)

    assembly = plan.get("assembly") or {}
    assembly_relative = assembly.get("output")
    assembly_path = shared_root / assembly_relative if assembly_relative and assembly_relative.replace("\\", "/").startswith("output/") else shared_root / "output" / assembly_relative if assembly_relative else Path("")
    assembly_report = inspect_media(assembly_path, float(plan.get("target_duration_seconds") or 126.97))
    report = {
        "schema_version": 1,
        "plan": str(plan_path),
        "assembly": assembly_report,
        "segments": segments,
        "summary": {
            "segment_count": len(segments),
            "existing_files": sum(1 for item in segments if item["exists"]),
            "segment_issue_count": sum(bool(item["issues"]) for item in segments),
            "assembly_issue_count": len(assembly_report["issues"]),
            "review_required": True,
            "note": "技术可播放性检查不等于角色连续性、口型、表演或艺术审核通过。",
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("shared_root", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = verify_delivery(args.plan, args.shared_root, args.report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if report["summary"]["existing_files"] == report["summary"]["segment_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
