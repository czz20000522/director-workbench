"""Pad the approved full audio candidate and cut exact per-segment assets.

The source video and decoded AAC stream can differ by a few audio frames. This
tool preserves the measured audio duration, pads only the delivery tail to the
declared picture duration, and records both values in the plan manifest.
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path
from typing import Any


def read_wav(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(params.nframes)
    if params.comptype != "NONE" or params.sampwidth != 2:
        raise ValueError(f"Only PCM-16 WAV is supported: {path}")
    return params, frames


def write_wav(path: Path, params: wave._wave_params, frames: bytes) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setparams(params)
        handle.writeframes(frames)


def slice_frames(frames: bytes, params: wave._wave_params, start: float, end: float) -> bytes:
    start_frame = round(start * params.framerate)
    end_frame = round(end * params.framerate)
    frame_bytes = params.nchannels * params.sampwidth
    available = len(frames) // frame_bytes
    start_frame = max(0, start_frame)
    raw_end = max(start_frame, end_frame)
    clipped_end = min(raw_end, available)
    result = frames[start_frame * frame_bytes : clipped_end * frame_bytes]
    if clipped_end < raw_end:
        result += b"\x00" * ((raw_end - clipped_end) * frame_bytes)
    return result


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--full-master", type=Path, required=True)
    parser.add_argument("--full-guide", type=Path, required=True)
    parser.add_argument("--delivery-output", type=Path, required=True)
    parser.add_argument("--guide-dir", type=Path, required=True)
    parser.add_argument("--segment-dir", type=Path, required=True)
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    args = parser.parse_args()

    plan: dict[str, Any] = json.loads(args.plan.read_text(encoding="utf-8"))
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Plan has no segments")
    master_params, master_frames = read_wav(args.full_master)
    guide_params, guide_frames = read_wav(args.full_guide)
    format_fields = ("nchannels", "sampwidth", "framerate", "comptype")
    if any(getattr(master_params, field) != getattr(guide_params, field) for field in format_fields):
        raise ValueError("Delivery master and guide WAV formats differ")
    target_duration = float(plan["target_delivery"]["duration_seconds"])
    target_frames = round(target_duration * master_params.framerate)
    frame_bytes = master_params.nchannels * master_params.sampwidth
    measured_frames = len(master_frames) // frame_bytes
    if measured_frames > target_frames:
        raise ValueError(f"Audio is longer than picture target: {measured_frames} > {target_frames} frames")
    padded_master = master_frames + b"\x00" * ((target_frames - measured_frames) * frame_bytes)
    write_wav(args.delivery_output, master_params, padded_master)

    updated_segments: list[dict[str, Any]] = []
    for segment in segments:
        segment_id = str(segment["id"])
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        delivery_path = args.segment_dir / f"{segment_id}-mix-v3A.wav"
        guide_path = args.guide_dir / f"{segment_id}-guide-v3A.wav"
        write_wav(delivery_path, master_params, slice_frames(padded_master, master_params, start, end))
        write_wav(guide_path, guide_params, slice_frames(guide_frames, guide_params, start, end))
        copy = dict(segment)
        audio = dict(copy.get("audio") or {})
        audio.update({
            "delivery_master": relative_path(delivery_path, args.shared_root),
            "guide": relative_path(guide_path, args.shared_root),
            "range_seconds": [start, end],
            "sample_rate": master_params.framerate,
            "frames": round((end - start) * master_params.framerate),
            "status": "prepared",
        })
        copy["audio"] = audio
        copy["notes"] = "完整 v3-A 音频已准备；首尾帧、提示词和 ComfyUI 工作流仍待补齐。"
        updated_segments.append(copy)

    updated = dict(plan)
    updated["schema_version"] = 2
    updated["status"] = "audio_ready"
    delivery = dict(updated.get("target_delivery") or {})
    delivery.update({
        "audio": relative_path(args.delivery_output, args.shared_root),
        "audio_status": "candidate_awaiting_listening",
        "audio_content_duration_seconds": measured_frames / master_params.framerate,
        "audio_delivery_duration_seconds": target_frames / master_params.framerate,
        "audio_tail_padding_seconds": (target_frames - measured_frames) / master_params.framerate,
        "audio_source": relative_path(args.full_master, args.shared_root),
        "audio_guide_source": relative_path(args.full_guide, args.shared_root),
    })
    updated["target_delivery"] = delivery
    updated["segments"] = updated_segments
    updated["blocking_inputs"] = [
        "逐段首帧与尾帧或上一段实际尾部上下文",
        "经验证的 ComfyUI UI/API 工作流",
        "资源预算与分批策略",
        "完整 v3-A 母版的人工听审与采用确认",
    ]
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "plan": str(args.plan_output),
        "delivery_master": str(args.delivery_output),
        "measured_duration_seconds": measured_frames / master_params.framerate,
        "delivery_duration_seconds": target_frames / master_params.framerate,
        "tail_padding_seconds": (target_frames - measured_frames) / master_params.framerate,
        "segment_count": len(updated_segments),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
