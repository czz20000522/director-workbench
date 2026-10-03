"""Validate that the canvas workflow is an exact, loadable view of the API recipe."""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_API = PROJECT / "workflows" / "示例作品翻唱-S02-v3A-ComfyUI-api-recipe.json"
DEFAULT_UI = PROJECT / "workflows" / "示例作品翻唱-S02-v3A-ComfyUI-ui-exact.json"
DEFAULT_PLAN = Path(r"D:\Comfy-Desktop\ComfyUI-Shared\output\示例系列工作目录\示例作品翻唱\导演台\示例作品-完整版分段计划-v2-audio-ready.json")


def history(prompt_id: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8188/history/{prompt_id}", timeout=8) as response:
            payload = json.load(response)
        return payload.get(prompt_id) or next(iter(payload.values()), None)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="核对 ComfyUI UI 工作流与 API 配方的执行等价性")
    parser.add_argument("api", type=Path, nargs="?", default=DEFAULT_API)
    parser.add_argument("ui", type=Path, nargs="?", default=DEFAULT_UI)
    parser.add_argument("--asset-id", default="S02")
    parser.add_argument("--prompt-id")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args()
    api_path = args.api.resolve()
    ui_path = args.ui.resolve()
    api = json.loads(api_path.read_text(encoding="utf-8"))
    ui = json.loads(ui_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    nodes = ui.get("nodes", [])
    mapping = {str(node.get("properties", {}).get("director_workbench_api_id")): node for node in nodes}
    if set(mapping) != set(api):
        errors.append(f"节点集合不一致：UI={len(mapping)} API={len(api)}")

    for api_id, api_node in api.items():
        node = mapping.get(api_id)
        if not node:
            continue
        if node.get("type") != api_node.get("class_type"):
            errors.append(f"{api_id}: class_type 不一致")
        if node.get("properties", {}).get("director_workbench_api_inputs") != api_node.get("inputs", {}):
            errors.append(f"{api_id}: API 输入快照不一致")

    ui_links: set[tuple[str, str, int, str]] = set()
    link_by_id = {int(link[0]): link for link in ui.get("links", []) if isinstance(link, list) and len(link) >= 6}
    for link_id, link in link_by_id.items():
        source = next((str(node.get("properties", {}).get("director_workbench_api_id")) for node in nodes if node.get("id") == link[1]), None)
        target_node = next((node for node in nodes if node.get("id") == link[3]), None)
        target = str(target_node.get("properties", {}).get("director_workbench_api_id")) if target_node else None
        if source is not None and target is not None and target_node:
            target_inputs = target_node.get("inputs", [])
            target_name = target_inputs[int(link[4])].get("name") if int(link[4]) < len(target_inputs) else "?"
            ui_links.add((source, target, int(link[2]), str(target_name)))

    api_links: set[tuple[str, str, int, str]] = set()
    for target, node in api.items():
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                input_name = next(name for name, candidate in node["inputs"].items() if candidate is value)
                api_links.add((value[0], target, int(value[1]), input_name))
    normalized_ui_links = ui_links
    if normalized_ui_links != api_links:
        errors.append(f"连接集合不一致：UI={len(normalized_ui_links)} API={len(api_links)}")

    executed: list[dict[str, Any]] = []
    if args.prompt_id:
        record = history(args.prompt_id)
        executed.append({"segment": args.asset_id, "prompt_id": args.prompt_id, "status": (record or {}).get("status", {}).get("status_str") if record else None})
    elif args.plan.exists():
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        for segment in plan.get("segments", []):
            if segment.get("id") != args.asset_id:
                continue
            prompt_id = segment.get("comfyui_task_id")
            record = history(str(prompt_id)) if prompt_id else None
            executed.append({"segment": segment.get("id"), "prompt_id": prompt_id, "status": (record or {}).get("status", {}).get("status_str") if record else None})

    report = {
        "valid": not errors,
        "ui_workflow": str(ui_path.relative_to(PROJECT)).replace("\\", "/"),
        "api_recipe": str(api_path.relative_to(PROJECT)).replace("\\", "/"),
        "ui_nodes": len(nodes),
        "api_nodes": len(api),
        "ui_links": len(normalized_ui_links),
        "api_links": len(api_links),
        "execution_evidence": executed,
        "errors": errors,
        "limitation": "ComfyUI 当前 LoadImage/LoadAudio 选择器只列输入根目录文件；导入后如出现媒体输入提示，需在画布节点中重新选择或上传对应素材。API recipe 的实际执行图和 UI 图的节点/连接/参数已逐项核对。",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
