"""Build a ComfyUI graph for reviewable video restoration and 2x upscale.

This is a post-picture-review adapter. It keeps ComfyUI as the executor and
emits an API graph that can also be opened in current ComfyUI builds. Run it on
representative shots before committing an entire project to the same finish.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT / "workflows" / "示例作品翻唱" / "finish-review" / "S05-upscale-api.json"


def build_graph(
    source: str,
    output_prefix: str,
    *,
    model_name: str = "RealESRGAN_x2plus.pth",
    color_reference: str | None = None,
    color_method: str = "uniform",
    color_strength: float = 0.2,
) -> dict[str, dict]:
    if color_method not in {"uniform", "per_frame"}:
        raise ValueError("color_method must be uniform or per_frame")
    if not 0.0 <= color_strength <= 1.0:
        raise ValueError("color_strength must stay within 0..1")

    graph: dict[str, dict] = {
        "load-video": {
            "class_type": "LoadVideo",
            "inputs": {"file": source},
            "_meta": {"title": "加载已审核片段"},
        },
        "video-components": {
            "class_type": "GetVideoComponents",
            "inputs": {"video": ["load-video", 0]},
            "_meta": {"title": "拆分画面、音频与帧率"},
        },
        "load-upscale-model": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": model_name},
            "_meta": {"title": "加载 2x 超分模型"},
        },
        "upscale-frames": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {
                "upscale_model": ["load-upscale-model", 0],
                "image": ["video-components", 0],
            },
            "_meta": {"title": "逐帧 2x 超分候选"},
        },
    }

    finished_frames: list[str | int] = ["upscale-frames", 0]
    if color_reference:
        graph["load-color-reference"] = {
            "class_type": "LoadImage",
            "inputs": {"image": color_reference},
            "_meta": {"title": "加载已批准色彩参考"},
        }
        graph["color-match"] = {
            "class_type": "ColorTransfer",
            "inputs": {
                "image_target": finished_frames,
                "image_ref": ["load-color-reference", 0],
                "method": "reinhard_lab",
                "source_stats": color_method,
                "strength": color_strength,
            },
            "_meta": {"title": f"色彩对齐候选 · {color_method} · {color_strength:.2f}"},
        }
        finished_frames = ["color-match", 0]

    graph["create-review-video"] = {
        "class_type": "CreateVideo",
        "inputs": {
            "images": finished_frames,
            "audio": ["video-components", 1],
            "fps": ["video-components", 2],
            "bit_depth": ["video-components", 3],
            "color_space": ["video-components", 4],
            "codec": "none",
        },
        "_meta": {"title": "重建音画同步审核候选"},
    }
    graph["save-review-video"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["create-review-video", 0],
            "filename_prefix": output_prefix,
            "format": "auto",
            "codec": "h264",
        },
        "_meta": {"title": "保存修复与超分候选"},
    }
    return graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="ComfyUI LoadVideo 路径，可使用 [output] 标注")
    parser.add_argument("--output-prefix", required=True, help="ComfyUI output 下的文件名前缀")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default="RealESRGAN_x2plus.pth")
    parser.add_argument("--color-reference", help="可选的已批准参考图，可使用 [output] 标注")
    parser.add_argument("--color-method", choices=["uniform", "per_frame"], default="uniform")
    parser.add_argument("--color-strength", type=float, default=0.2)
    args = parser.parse_args()

    graph = build_graph(
        args.source,
        args.output_prefix,
        model_name=args.model,
        color_reference=args.color_reference,
        color_method=args.color_method,
        color_strength=args.color_strength,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "nodes": len(graph)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
