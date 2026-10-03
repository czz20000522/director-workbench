"""Build a ComfyUI UI workflow from the exact API recipe.

The API recipe is the execution source of truth.  This converter creates the
node-and-link representation used by the ComfyUI canvas while retaining the
same class types, scalar inputs and links.  It also writes a small mapping in
``extra.director_workbench`` so a validator can prove that the UI file came
from, and round-trips to, the recipe that was actually submitted.
"""
from __future__ import annotations

import argparse
import copy
import json
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\Comfy-Desktop")
PROJECT = ROOT / "ComfyUI-Workspace" / "projects" / "director-workbench"
DEFAULT_API = PROJECT / "workflows" / "示例作品翻唱-S02-v3A-ComfyUI-api-recipe.json"
DEFAULT_UI = PROJECT / "workflows" / "示例作品翻唱-S02-v3A-ComfyUI-ui-exact.json"
COMFY_OBJECT_INFO = "http://127.0.0.1:8188/object_info"
COMFY_INPUT = ROOT / "ComfyUI-Shared" / "input"


def object_info() -> dict[str, Any]:
    with urllib.request.urlopen(COMFY_OBJECT_INFO, timeout=10) as response:
        return json.load(response)


def input_order(info: dict[str, Any], api_inputs: dict[str, Any]) -> list[str]:
    declared = list(info.get("input_order", {}).get("required", []))
    declared += list(info.get("input_order", {}).get("optional", []))
    # Some nodes expose virtual UI-only inputs (for example a resolution
    # preview or an autogrow container). They are not part of the submitted
    # API graph and must not become null widgets in the saved canvas file.
    order = [name for name in declared if name in api_inputs]
    # Dynamic SaveVideo inputs and newer custom nodes can expose names that
    # are not present in the object-info order. Keep them visible as widgets.
    for name in api_inputs:
        if name not in order and name not in {"video-preview"}:
            order.append(name)
    return order


def input_type(spec: Any, value: Any) -> str:
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return "COMBO"
    if isinstance(spec, list) and spec and isinstance(spec[0], str):
        return spec[0]
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "FLOAT"
    return "STRING"


def ui_widget_value(class_type: str, name: str, value: Any) -> Any:
    """Use the filename form that ComfyUI's canvas input selectors expect."""
    if class_type in {"LoadImage", "LoadAudio"} and name in {"image", "audio"} and isinstance(value, str):
        candidate = Path(value)
        try:
            return candidate.relative_to(COMFY_INPUT).as_posix()
        except ValueError:
            return value
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="从 ComfyUI API 配方生成可打开的画布工作流")
    parser.add_argument("api", type=Path, nargs="?", default=DEFAULT_API)
    parser.add_argument("ui", type=Path, nargs="?", default=DEFAULT_UI)
    parser.add_argument("--project", default="示例作品翻唱")
    parser.add_argument("--asset-id", default="S02")
    parser.add_argument("--workflow-id")
    args = parser.parse_args()
    api_path = args.api.resolve()
    ui_path = args.ui.resolve()
    api_graph = json.loads(api_path.read_text(encoding="utf-8"))
    info = object_info()
    api_ids = list(api_graph)
    numeric_ids = {api_id: index + 1 for index, api_id in enumerate(api_ids)}
    links: list[list[Any]] = []
    link_for_input: dict[tuple[str, str], int] = {}
    outgoing: dict[tuple[str, int], list[int]] = {}

    for api_id, node in api_graph.items():
        for name, value in node.get("inputs", {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                link_id = len(links) + 1
                source_id, source_slot = value
                link_for_input[(api_id, name)] = link_id
                links.append([link_id, numeric_ids[source_id], int(source_slot), numeric_ids[api_id], 0, "*UNSPECIFIED*"])
                outgoing.setdefault((source_id, int(source_slot)), []).append(link_id)

    nodes: list[dict[str, Any]] = []
    for index, (api_id, node) in enumerate(api_graph.items()):
        class_type = str(node["class_type"])
        node_info = info.get(class_type, {})
        specs = {}
        for section in ("required", "optional"):
            specs.update(node_info.get("input", {}).get(section, {}))
        names = input_order(node_info, node.get("inputs", {}))
        ui_inputs: list[dict[str, Any]] = []
        widgets: list[Any] = []
        for slot, name in enumerate(names):
            if name == "video-preview":
                continue
            value = node.get("inputs", {}).get(name)
            spec = specs.get(name)
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                ui_inputs.append({"name": name, "type": input_type(spec, value), "link": link_for_input[(api_id, name)]})
                links[link_for_input[(api_id, name)] - 1][4] = len(ui_inputs) - 1
            else:
                ui_inputs.append({"name": name, "type": input_type(spec, value), "widget": {"name": name}})
                widgets.append(ui_widget_value(class_type, name, value))

        output_names = node_info.get("output_name", [])
        if isinstance(output_names, str):
            output_names = [output_names]
        output_types = node_info.get("output", [])
        ui_outputs = []
        for output_slot, output_name in enumerate(output_names):
            ui_outputs.append({
                "name": output_name,
                "type": output_types[output_slot] if output_slot < len(output_types) else "*",
                "links": outgoing.get((api_id, output_slot)),
            })

        nodes.append({
            "id": numeric_ids[api_id],
            "type": class_type,
            "pos": [120 + (index % 4) * 420, 120 + (index // 4) * 260],
            "size": [320, 220],
            "flags": {},
            "order": index,
            "mode": 0,
            "inputs": ui_inputs,
            "outputs": ui_outputs,
            "properties": {
                "director_workbench_api_id": api_id,
                "director_workbench_api_inputs": copy.deepcopy(node.get("inputs", {})),
            },
            "widgets_values": widgets,
            "_meta": copy.deepcopy(node.get("_meta", {"title": class_type})),
        })

    # The UI graph stores target slots in the generated input order.  Fill the
    # link type after all nodes are known, using the target input type.
    input_types = {(node["id"], inp["link"]): inp["type"] for node in nodes for inp in node["inputs"] if inp.get("link")}
    for link in links:
        link[5] = input_types.get((link[3], link[0]), "*")

    workflow = {
        "id": args.workflow_id or f"director-workbench-{args.asset_id.lower()}-exact",
        "revision": 0,
        "last_node_id": max(numeric_ids.values()),
        "last_link_id": len(links),
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {
            "director_workbench": {
                "project": args.project,
                "asset_id": args.asset_id,
                "snapshot_role": "ui_exact_from_api_recipe",
                "equivalence": "exact_api_graph_roundtrip",
                "api_recipe": api_path.relative_to(PROJECT).as_posix(),
                "api_node_id_map": numeric_ids,
                "note": "可在 ComfyUI 画布中打开；执行等价性由 validate_exact_ui_workflow.py 核对。",
            }
        },
        "version": 0.4,
    }
    ui_path.parent.mkdir(parents=True, exist_ok=True)
    ui_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(ui_path)


if __name__ == "__main__":
    main()
