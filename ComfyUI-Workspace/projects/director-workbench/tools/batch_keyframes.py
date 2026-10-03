"""Generate a small, auditable batch of segment keyframes through ComfyUI.

The workbench owns the plan and records the outputs. Qwen Image Edit remains
the renderer; this helper never loads or unloads models and submits one image
job at a time so the same loaded model can be reused safely.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(r"D:\Comfy-Desktop")
PLAN = ROOT / "ComfyUI-Shared" / "output" / "示例系列工作目录" / "示例作品翻唱" / "导演台" / "示例作品-完整版分段计划-v2-audio-ready.json"
INPUT_ROOT = ROOT / "ComfyUI-Shared" / "input"
OUTPUT_ROOT = ROOT / "ComfyUI-Shared" / "output"
PROJECT = ROOT / "ComfyUI-Workspace" / "projects" / "director-workbench"
RUNTIME = ROOT / "ComfyUI-Workspace" / "runtime" / "director-workbench"
BASE_IMAGE = INPUT_ROOT / "示例系列工作目录" / "示例作品翻唱" / "动态试作" / "first-frame-v1.png"
QWEN_RECIPE_DIR = PROJECT / "workflows" / "示例作品翻唱" / "keyframes-v2"
OUTPUT_RELATIVE = Path("output/示例系列工作目录/示例作品翻唱/导演台/完整版-v2/keyframes")


def request_json(api_url: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(api_url.rstrip("/") + path, data=data, method="POST" if data is not None else "GET", headers={"Content-Type": "application/json"} if data is not None else {})
    with urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def image_graph(image: Path, prompt: str, prefix: str, seed: int) -> dict:
    return {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_edit_2511_int8_convrot.safetensors", "weight_dtype": "default"}},
        "5": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["9", 0], "reference_latents_method": "index_timestep_zero"}},
        "6": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["13", 0], "reference_latents_method": "index_timestep_zero"}},
        "7": {"class_type": "CFGNorm", "inputs": {"model": ["2", 0], "strength": 1.0}},
        "9": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 0], "prompt": "low quality, blurry, deformed, extra characters, text, watermark, pumpkin, hat, oversized head object"}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["3", 0]}},
        "13": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 0], "vae": ["3", 0], "image1": ["16", 0], "prompt": prompt}},
        "14": {"class_type": "VAEEncode", "inputs": {"pixels": ["16", 0], "vae": ["3", 0]}},
        "15": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "positive": ["6", 0], "negative": ["5", 0], "latent_image": ["14", 0], "seed": seed, "steps": 20, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "16": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["17", 0]}},
        "17": {"class_type": "LoadImage", "inputs": {"image": str(image.resolve())}},
        "18": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": prefix}},
    }


def prompt_for(segment: dict, frame: str) -> str:
    phase = "opening pose at the start of the sung phrase" if frame == "first" else "ending pose at the end of the sung phrase"
    motion = "one paw gently lifted to mark the beat" if frame == "first" else "one paw lowered slightly after the beat, a soft natural smile"
    return (
        f"A vertical 9:16 single character keyframe for a sweet singing video, {phase}. "
        "Keep the exact same small yellow plush capybara girl identity from the input image: yellow plush silhouette, orange muzzle, eyelashes, pink bow, and one tiny smooth orange head ornament. "
        "The head ornament must remain small and simple; absolutely no pumpkin, no hat, no large orange object, and no extra headwear. "
        f"Set the scene in {segment['location']} with {segment['shot_size']} framing and {segment['camera']} camera intention. "
        f"She wears {segment['wardrobe']}; {motion}. "
        "Warm expressive singing face, stable plush texture, clean composition, no other characters, no text, no microphone."
    )


def wait_for_image(api_url: str, prompt_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = request_json(api_url, f"/history/{prompt_id}")
        entry = history.get(prompt_id)
        if entry and entry.get("status", {}).get("status_str") in {"success", "error"}:
            return entry
        time.sleep(2)
    raise TimeoutError(f"等待 ComfyUI 图像任务 {prompt_id} 超时")


def sample(api_url: str) -> dict:
    try:
        stats = request_json(api_url, "/system_stats")
        queue = request_json(api_url, "/queue")
        system = stats.get("system", {})
        device = (stats.get("devices") or [{}])[0]
        return {"timestamp": time.time(), "gpu_free_mib": round(device.get("vram_free", 0) / 2**20), "gpu_total_mib": round(device.get("vram_total", 0) / 2**20), "ram_free_gib": round(system.get("ram_free", 0) / 2**30, 2), "queue_running": len(queue.get("queue_running", [])), "queue_pending": len(queue.get("queue_pending", []))}
    except Exception as exc:
        return {"timestamp": time.time(), "error": str(exc)}


def output_from_history(entry: dict) -> str:
    for output in entry.get("outputs", {}).values():
        for item in output.get("images", []):
            filename = str(item.get("filename", ""))
            subfolder = str(item.get("subfolder", "")).strip("/\\")
            relative = Path(subfolder) / filename if subfolder else Path(filename)
            candidate = OUTPUT_ROOT / relative
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError("ComfyUI history 成功但没有找到图片输出")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8188")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--plan", type=Path, default=PLAN)
    args = parser.parse_args()
    if not BASE_IMAGE.is_file():
        raise FileNotFoundError(BASE_IMAGE)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    segments = plan["segments"][args.start - 1: args.start - 1 + args.limit]
    RUNTIME.mkdir(parents=True, exist_ok=True)
    QWEN_RECIPE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME / "keyframes-v2-resource.jsonl"
    results = []
    for segment in segments:
        segment_dir = OUTPUT_ROOT / OUTPUT_RELATIVE.relative_to("output")
        segment_dir.mkdir(parents=True, exist_ok=True)
        for offset, frame in enumerate(("first", "last")):
            existing = segment.get("keyframes", {}).get(frame)
            if existing:
                candidate = ROOT / "ComfyUI-Shared" / existing if existing.startswith("output/") else Path(existing)
                if candidate.is_file():
                    results.append({"segment": segment["id"], "frame": frame, "status": "existing", "path": str(candidate)})
                    continue
            # SaveVideo resolves filename_prefix relative to ComfyUI's output
            # root; the plan path adds the explicit `output/` component only
            # when recording the resulting asset.
            prefix = str(OUTPUT_RELATIVE.relative_to("output") / f"{segment['id']}-{frame}").replace("\\", "/")
            seed = 340921 + int(segment["id"][1:]) * 100 + offset
            prompt = prompt_for(segment, frame)
            graph = image_graph(BASE_IMAGE, prompt, prefix, seed)
            recipe_path = QWEN_RECIPE_DIR / f"{segment['id']}-{frame}-qwen2511-api.json"
            recipe_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            before = sample(args.api_url)
            submitted = request_json(args.api_url, "/prompt", {"prompt": graph, "client_id": f"director-keyframes-{uuid.uuid4()}"})
            prompt_id = submitted.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI 未返回 prompt_id: {submitted}")
            entry = wait_for_image(args.api_url, prompt_id, args.timeout)
            after = sample(args.api_url)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"segment": segment["id"], "frame": frame, "prompt_id": prompt_id, "before": before, "after": after}, ensure_ascii=False) + "\n")
            if entry.get("status", {}).get("status_str") != "success":
                raise RuntimeError(f"{segment['id']} {frame} ComfyUI 失败: {entry.get('status')}")
            path = output_from_history(entry)
            relative = str(Path(path).relative_to(OUTPUT_ROOT)).replace("\\", "/")
            segment.setdefault("keyframes", {})[frame] = f"output/{relative}"
            segment.setdefault("keyframe_recipes", {})[frame] = str(recipe_path)
            results.append({"segment": segment["id"], "frame": frame, "status": "generated", "prompt_id": prompt_id, "path": path})
            args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        first_ref = segment.get("keyframes", {}).get("first")
        last_ref = segment.get("keyframes", {}).get("last")
        if first_ref and last_ref:
            segment["status"] = "keyframes_prepared"
            segment["notes"] = "完整 v3-A 音频和首尾帧已准备；视频工作流与视频生成仍待提交。"
    plan["keyframe_batch"] = {"status": "partial" if any(item["status"] == "generated" for item in results) else "unchanged", "last_run_at": time.time(), "renderer": "ComfyUI / Qwen Image Edit 2511", "resource_log": str(log_path)}
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"results": results, "plan": str(args.plan), "resource_log": str(log_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
