"""Submit planned singing segments to ComfyUI MiniMax H3 one at a time."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(r"D:\Comfy-Desktop")
SHARED = ROOT / "ComfyUI-Shared"
INPUT_ROOT = SHARED / "input"
OUTPUT_ROOT = SHARED / "output"
PROJECT = ROOT / "ComfyUI-Workspace" / "projects" / "director-workbench"
RUNTIME = ROOT / "ComfyUI-Workspace" / "runtime" / "director-workbench"
PLAN = OUTPUT_ROOT / "示例系列工作目录" / "示例作品翻唱" / "导演台" / "示例作品-完整版分段计划-v2-audio-ready.json"
TEMPLATE = PROJECT / "workflows\示例作品翻唱-S02-v3A-ComfyUI-api-recipe.json"
H3_RENDER = ROOT / ".agents" / "skills" / "aigc-video-production" / "scripts" / "h3_render.py"

spec = importlib.util.spec_from_file_location("director_h3_render", H3_RENDER)
if spec is None or spec.loader is None:
    raise RuntimeError(f"无法加载 H3 适配器: {H3_RENDER}")
h3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h3)


def request_json(api_url: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(api_url.rstrip("/") + path, data=data, method="POST" if data is not None else "GET", headers={"Content-Type": "application/json"} if data is not None else {})
    with urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def sample(api_url: str) -> dict:
    try:
        stats = request_json(api_url, "/system_stats")
        queue = request_json(api_url, "/queue")
        system = stats.get("system", {})
        device = (stats.get("devices") or [{}])[0]
        return {"timestamp": time.time(), "gpu_free_mib": round(device.get("vram_free", 0) / 2**20), "gpu_total_mib": round(device.get("vram_total", 0) / 2**20), "ram_free_gib": round(system.get("ram_free", 0) / 2**30, 2), "queue_running": len(queue.get("queue_running", [])), "queue_pending": len(queue.get("queue_pending", []))}
    except Exception as exc:
        return {"timestamp": time.time(), "error": str(exc)}


def shared_path(reference: str) -> Path:
    path = Path(reference.replace("/", "\\"))
    return path if path.is_absolute() else SHARED / path


def comfy_input_path(reference: str) -> Path:
    """Resolve a generated output reference to its registered input copy."""
    path = shared_path(reference)
    marker = f"{os.sep}output{os.sep}"
    normalized = str(path).replace("/", "\\")
    if marker in normalized:
        relative = Path(normalized.split(marker, 1)[1])
        candidate = INPUT_ROOT / relative
        if candidate.is_file():
            return candidate
    return path


def singing_prompt(segment: dict) -> str:
    return (
        "One continuous five-second intimate music performance in a vertical 9:16 frame. "
        "The same small yellow plush capybara girl sings the supplied vocal guide with a sweet, emotionally present voice and visible syllable-shaped mouth movement. "
        f"Scene: {segment['location']}. Shot size: {segment['shot_size']}. Camera intention: {segment['camera']}. Wardrobe: {segment['wardrobe']}. "
        "Keep the exact yellow plush silhouette, orange muzzle, eyelashes, pink bow and small orange head ornament consistent with the input frames. "
        "Use a natural singing gesture and a gentle expression change; keep feet grounded, preserve the scene and clothing, no extra characters, no captions, no microphone, no spoken recitation."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8188")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--allow-short-candidate", action="store_true", help="为不足 5 秒的尾段生成 5 秒候选，后续必须由 ComfyUI 装配流程裁切")
    parser.add_argument("--plan", type=Path, default=PLAN)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    segments = plan["segments"][args.start - 1: args.start - 1 + args.limit]
    recipe_dir = PROJECT / "workflows" / "示例作品翻唱" / "h3-v2"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME / "h3-v2-resource.jsonl"
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    results = []
    for segment in segments:
        if segment.get("video", {}).get("path"):
            results.append({"segment": segment["id"], "status": "existing", "path": segment["video"]["path"]})
            continue
        planned_duration = float(segment.get("duration_seconds", 5))
        if planned_duration not in (5, 10, 15) and not args.allow_short_candidate:
            segment["status"] = "video_pending_trim"
            segment["notes"] = "音频时长不足 H3 最小 5 秒；需先生成 5 秒候选，再由 ComfyUI 装配工作流按采样帧裁切。"
            results.append({"segment": segment["id"], "status": "pending_trim"})
            continue
        first_ref = segment.get("keyframes", {}).get("first")
        last_ref = segment.get("keyframes", {}).get("last")
        guide_ref = segment.get("audio", {}).get("guide")
        if not first_ref or not last_ref or not guide_ref:
            raise RuntimeError(f"{segment['id']} 缺少首帧、尾帧或音频引导")
        first, last, guide = comfy_input_path(first_ref), comfy_input_path(last_ref), shared_path(guide_ref)
        missing = [str(path) for path in (first, last, guide) if not path.is_file()]
        if missing:
            raise FileNotFoundError("缺少 H3 输入: " + "; ".join(missing))
        prompt = singing_prompt(segment)
        seed = 810000 + int(segment["id"][1:])
        prefix = f"示例系列工作目录/示例作品翻唱/导演台/完整版-v2/video/{segment['id']}-v3A"
        render_duration = int(planned_duration) if planned_duration in (5, 10, 15) else 5
        graph = h3.prepare_h3_graph(template, prompt=prompt, first_frame=str(first), last_frame=str(last), duration_seconds=render_duration, seed=seed, filename_prefix=prefix, aspect_ratio="9:16 (Portrait Widescreen)", megapixels=0.4, turbo=True, audio_guide=str(guide), preserve_source_audio=False)
        recipe_path = recipe_dir / f"{segment['id']}-v3A-api-recipe.json"
        recipe_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = sample(args.api_url)
        job = h3.submit_shot(api_url=args.api_url, template_path=TEMPLATE, prompt=prompt, first_frame=str(first), last_frame=str(last), duration_seconds=render_duration, seed=seed, filename_prefix=prefix, aspect_ratio="9:16 (Portrait Widescreen)", megapixels=0.4, turbo=True, audio_guide=str(guide), preserve_source_audio=False)
        prompt_id = job["prompt_id"]
        entry = h3.wait_for_prompt(prompt_id, api_url=args.api_url, timeout_seconds=args.timeout)
        after = sample(args.api_url)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"segment": segment["id"], "prompt_id": prompt_id, "before": before, "after": after}, ensure_ascii=False) + "\n")
        video = h3.video_output_from_history(entry)
        if entry.get("status", {}).get("status_str") != "success" or not video:
            raise RuntimeError(f"{segment['id']} H3 失败: {entry.get('status')}")
        segment["prompt"] = prompt
        segment["workflow"] = str(recipe_path)
        segment["comfyui_task_id"] = prompt_id
        segment["video"] = {"path": f"output/{video}", "variant": "v3-A"}
        segment["status"] = "video_pending_trim" if render_duration != planned_duration else "video_generated"
        segment["notes"] = ("ComfyUI history 返回成功；这是 5 秒尾段候选，必须在 ComfyUI 装配流程中裁切到计划时长；候选视频待导演台审核。" if render_duration != planned_duration else "ComfyUI history 返回成功；候选视频待导演台审核，尚未视为采用版本。")
        args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append({"segment": segment["id"], "status": "generated", "prompt_id": prompt_id, "video": video})
    plan["h3_batch"] = {"status": "partial", "last_run_at": time.time(), "renderer": "ComfyUI / MiniMax H3", "resource_log": str(log_path)}
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"results": results, "plan": str(args.plan), "resource_log": str(log_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
