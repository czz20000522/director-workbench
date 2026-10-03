"""Build a ComfyUI API graph for the approved segment assembly.

The workbench records the segment plan; ComfyUI still performs demux, frame
trim, video creation, audio attachment, concatenation, and saving. This helper
only materializes that graph so it can be imported into the ComfyUI UI or
submitted through its API.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(r"D:\Comfy-Desktop")
SHARED = ROOT / "ComfyUI-Shared"
OUTPUT_ROOT = SHARED / "output"
PROJECT = ROOT / "ComfyUI-Workspace" / "projects" / "director-workbench"
DEFAULT_PLAN = OUTPUT_ROOT / "示例系列工作目录" / "示例作品翻唱" / "导演台" / "示例作品-完整版分段计划-v2-audio-ready.json"
DEFAULT_AUDIO = str(SHARED / "input" / "示例系列工作目录/示例作品翻唱/导演台/完整版-v2/audio/lumei-sweeter-v4-full-A-delivery.wav")


def input_reference(output_reference: str) -> str:
    if Path(output_reference).is_absolute():
        return str(Path(output_reference))
    normalized = output_reference.replace("\\", "/")
    if normalized.startswith("output/"):
        return f"{normalized[len('output/'):]} [output]"
    if normalized.startswith("input/"):
        return str(SHARED / normalized)
    if normalized.startswith("ComfyUI-Shared/"):
        return str(ROOT / normalized)
    if normalized.endswith((" [input]", " [output]")):
        return normalized
    input_path = SHARED / "input" / normalized
    if input_path.is_file():
        # Prefer the staged input copy when it exists so the graph remains
        # importable in the ordinary ComfyUI input browser.
        return str(input_path)
    output_path = SHARED / "output" / normalized
    if output_path.is_file():
        # ComfyUI's annotated path lets LoadVideo/LoadAudio read the current
        # output candidate directly after a segment has been regenerated.
        return f"{normalized} [output]"
    # Keep a deterministic input-root path in a not-yet-generated graph; the
    # subsequent ComfyUI validation will report the missing source clearly.
    return str(input_path)


def trim_spec(segment: dict, fps: float = 24.0) -> dict:
    """Same round-to-frame contract as installed MiniMaxH3OutputTrimT8."""
    seconds = float(segment['duration_seconds'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('取片时长必须为有限正数')
    count = max(1, round(seconds * fps))
    return {'start_seconds': 0.0, 'duration_seconds': seconds, 'fps': fps,
            'frame_count': count, 'playback_seconds': round(count / fps, 6)}


def build_graph(plan: dict, audio_reference: str, filename_prefix: str, segment_ids: set[str] | None = None) -> dict:
    muted = ((plan.get('assembly_edit') or {}).get('sound') or {}).get('policy') == 'mute'
    if muted:
        audio_reference = ''  # Explicit mute overrides any old/default master binding.
    segments = plan["segments"]
    ids = [segment.get("id") for segment in segments]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("分镜 ID 必须非空且唯一")
    if segment_ids is not None:
        unknown = segment_ids - set(ids)
        if unknown:
            raise ValueError(f"计划中不存在所选分镜: {', '.join(sorted(unknown))}")
        segments = [segment for segment in segments if segment["id"] in segment_ids]
    if not segments:
        raise ValueError("计划中没有待装配的片段")
    missing = [segment['id'] for segment in segments if not segment.get('video', {}).get('path')]
    if missing:
        raise ValueError(f"以下分镜缺少视频，不能生成不完整成片: {', '.join(missing)}")
    for segment in segments:
        try:
            duration = float(segment['duration_seconds'])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{segment['id']} 缺少有效时长") from None
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"{segment['id']} 时长必须为有限正数")

    graph: dict[str, dict] = {}
    video_links: list[list[str | int]] = []
    for index, segment in enumerate(segments, start=1):
        segment_id = segment["id"]
        load_id = f"load-{segment_id}"
        components_id = f"components-{segment_id}"
        trim_id = f"trim-{segment_id}"
        create_id = f"create-{segment_id}"
        graph[load_id] = {
            "class_type": "LoadVideo",
            "inputs": {"file": input_reference(segment["video"]["path"])},
            "_meta": {"title": f"加载 {segment_id} 视频候选"},
        }
        graph[components_id] = {
            "class_type": "GetVideoComponents",
            "inputs": {"video": [load_id, 0]},
            "_meta": {"title": f"拆分 {segment_id} 音画"},
        }
        audio_link = [components_id, 1]
        segment_audio = segment.get("audio") or {}
        delivery_master = segment_audio.get("delivery_master") if isinstance(segment_audio, dict) else None
        if not muted and not audio_reference and delivery_master and str(delivery_master).strip():
            audio_id = f"load-delivery-audio-{segment_id}"
            graph[audio_id] = {
                "class_type": "LoadAudio",
                "inputs": {"audio": input_reference(str(delivery_master).strip())},
                "_meta": {"title": f"加载 {segment_id} 交付音轨"},
            }
            audio_link = [audio_id, 0]
        trim = trim_spec(segment)
        graph[trim_id] = {
            "class_type": "MiniMaxH3OutputTrimT8",
            "inputs": {
                "frames": [components_id, 0],
                "audio": audio_link,
                "start_seconds": trim['start_seconds'],
                "duration_seconds": trim['duration_seconds'],
                "fps": trim['fps'],
            },
            "_meta": {"title": f"按计划裁切 {segment_id} · {segment['duration_seconds']} 秒"},
        }
        graph[create_id] = {
            "class_type": "CreateVideo",
            "inputs": {
                "images": [trim_id, 0],
                "audio": [trim_id, 1],
                "fps": 24.0,
                "bit_depth": 8,
                "color_space": "sRGB",
                "codec": "none",
            },
            "_meta": {"title": f"重建 {segment_id} 精确时长片段"},
        }
        if muted:
            graph[trim_id]['inputs'].pop('audio', None)
            graph[create_id]['inputs'].pop('audio', None)
        video_links.append([create_id, 0])

    # V3 dynamic inputs are submitted with their full path. The server expands
    # `videos.video0` into the nested `videos={"video0": ...}` argument before
    # calling ConcatenateVideo.
    concat_inputs = {f"videos.video{index}": link for index, link in enumerate(video_links)}
    concat_inputs["codec"] = "auto"
    if audio_reference:
        concat_inputs["complete_audio"] = ["load-complete-audio", 0]
        graph["load-complete-audio"] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": audio_reference},
            "_meta": {"title": "加载全片音轨"},
        }
    graph["concatenate"] = {
        "class_type": "ConcatenateVideo",
        "inputs": concat_inputs,
        "_meta": {"title": f"按计划顺序拼接 {len(video_links)} 段"},
    }
    graph["save"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["concatenate", 0],
            "filename_prefix": filename_prefix,
            "format": "auto",
            "codec": "auto",
        },
        "_meta": {"title": "保存成片候选"},
    }
    return graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=PROJECT / "workflows" / "示例作品翻唱-完整版-v2-assembly-api.json")
    parser.add_argument("--audio", default=DEFAULT_AUDIO)
    parser.add_argument("--filename-prefix", default="示例系列工作目录/示例作品翻唱/导演台/完整版-v2/assembly")
    parser.add_argument("--segments", help="逗号分隔的片段 ID；省略则使用计划中已有视频的全部片段")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    ids = {item.strip() for item in args.segments.split(",")} if args.segments else None
    graph = build_graph(plan, args.audio, args.filename_prefix, ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "nodes": len(graph), "segments": sum(1 for node in graph if node.startswith("create-"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
