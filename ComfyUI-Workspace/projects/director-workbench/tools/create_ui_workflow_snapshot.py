"""Create a safe, work-specific ComfyUI UI workflow snapshot.

The installed template is visual-only for this handoff. The exact audio-guide
execution remains in the sibling API recipe. Never silently claim equivalence.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(r"D:\Comfy-Desktop")
TEMPLATE = ROOT / "ComfyUI-Installs" / "第一个comfyui配置" / "ComfyUI" / "user" / "default" / "workflows" / "图生视频.json"
DEST = ROOT / "ComfyUI-Workspace" / "projects" / "director-workbench" / "workflows" / "示例作品翻唱-S02-v3A-ComfyUI-ui-visual-snapshot.json"
PROMPT = "One continuous five-second intimate music performance. The same small yellow plush capybara girl in the dusty blue jacket and pink dress sings with sweet expressive emotion in the sunlit courtyard. Subtle breathing between phrases, her raised paw marks the repeating hu notes, one quick natural wink synchronized with a musical accent, then eyes reopen while singing continues. Stable face, orange muzzle, pink bow and orange ornament, daylight, no captions."


def main() -> None:
    workflow = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    workflow = copy.deepcopy(workflow)
    workflow.setdefault("extra", {})["director_workbench"] = {
        "project": "示例作品翻唱", "asset_id": "S02", "snapshot_role": "visual_ui_only",
        "equivalence": "not_exact_audio_guide_recipe",
        "note": "The exact ComfyUI submission with v3-A audio guide is saved beside this file as 示例作品翻唱-S02-v3A-ComfyUI-api-recipe.json.",
    }
    for node in workflow.get("nodes", []):
        node_type = node.get("type")
        values = node.get("widgets_values")
        if node_type == "LoadImage" and isinstance(values, list):
            values[0] = "示例系列工作目录/示例作品翻唱/动态试作/first-frame-v1.png"
        elif node_type == "ResolutionSelector" and isinstance(values, list):
            values[0], values[1] = "9:16 (Portrait Widescreen)", 0.4
        elif node_type == "SaveVideo" and isinstance(values, list):
            values[0] = "示例系列工作目录/示例作品翻唱/导演台/S02-v3A-ui-visual"
        elif node_type == "4c314f31-ecda-4b08-ae98-faaba1bf613f" and isinstance(values, list):
            values[0], values[1], values[2], values[3] = PROMPT, 480, 864, 5
            values[6] = 340921
            values[7:11] = ["minimax_h3_fl2va_pruned_int8_convrot.safetensors", "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors", "minimax_h3_video_vae_fp16.safetensors", "minimax_h3_audio_vae_fp32.safetensors"]
        elif node_type == "MarkdownNote" and node.get("title") == "Note: MiniMax H3":
            if isinstance(values, list) and values:
                values[0] = "## 示例作品 · S02\n\n导演台可视化快照：首帧、提示词、画幅和输出前缀已替换为本作品。\n\n注意：这份 UI 快照用于浏览器中的节点检查；实际 S02 任务还接入了 v3-A 音频引导，精确 API 配方见同目录的 API recipe 文件。"
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(DEST)


if __name__ == "__main__":
    main()
