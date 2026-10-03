"""Submit a local Qwen Image Edit graph to ComfyUI and wait for its image."""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


def request_json(base_url: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def reference_size(width: int, height: int) -> tuple[int, int]:
    """Keep the full composition near 1 MP, on the VAE's eight-pixel grid.

    Prefer an exact ratio when it fits 80–100% of the pixel budget (9:16
    becomes 720x1280). Otherwise only round each scaled edge to eight pixels.
    """
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError('Reference dimensions must be positive integers')
    total = 1024 * 1024
    divisor = math.gcd(width, height)
    unit_w, unit_h = width // divisor, height // divisor
    multiple = int(math.sqrt(total / (unit_w * unit_h)) // 8) * 8
    exact = (unit_w * multiple, unit_h * multiple)
    if exact[0] * exact[1] >= total * 0.8:
        return exact
    scale = math.sqrt(total / (width * height))
    return max(8, round(width * scale / 8) * 8), max(8, round(height * scale / 8) * 8)


def graph(image: Path, prompt: str, prefix: str, seed: int,
          reference_dimensions: tuple[int, int] | None = None) -> dict:
    # Capability discovery builds a graph without opening a reference file.
    width, height = reference_size(*(reference_dimensions or (1024, 1024)))
    return {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_edit_2511_int8_convrot.safetensors", "weight_dtype": "default"}},
        "5": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["9", 0], "reference_latents_method": "index_timestep_zero"}},
        "6": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["13", 0], "reference_latents_method": "index_timestep_zero"}},
        "7": {"class_type": "CFGNorm", "inputs": {"model": ["2", 0], "strength": 1.0}},
        "9": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 0], "vae": ["3", 0], "image1": ["16", 0], "prompt": "low quality, blurry, deformed, extra characters, text, watermark"}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["3", 0]}},
        "13": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 0], "vae": ["3", 0], "image1": ["16", 0], "prompt": prompt}},
        "14": {"class_type": "VAEEncode", "inputs": {"pixels": ["16", 0], "vae": ["3", 0]}},
        "15": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "positive": ["6", 0], "negative": ["5", 0], "latent_image": ["14", 0], "seed": seed, "steps": 20, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "16": {"class_type": "ImageScale", "inputs": {"image": ["17", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}},
        "17": {"class_type": "LoadImage", "inputs": {"image": str(image)}},
        "18": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": prefix}},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8188")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--seed", type=int, default=340921)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--recipe-output", type=Path)
    args = parser.parse_args()
    if not args.image.is_file():
        raise FileNotFoundError(args.image)
    import av  # Existing workbench dependency; graph/capability need no decoder.
    with av.open(str(args.image)) as media:
        frame = next(media.decode(video=0))
        dimensions = (frame.width, frame.height)
    prompt = graph(args.image.resolve(), args.prompt, args.prefix, args.seed, dimensions)
    if args.recipe_output:
        args.recipe_output.parent.mkdir(parents=True, exist_ok=True)
        args.recipe_output.write_text(json.dumps(prompt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    client_id = str(uuid.uuid4())
    submitted = request_json(args.api_url, "/prompt", {"prompt": prompt, "client_id": client_id})
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {submitted}")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        history = request_json(args.api_url, f"/history/{prompt_id}")
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {}).get("status_str")
            if status in {"success", "error"}:
                if status != "success":
                    raise RuntimeError(f"ComfyUI image edit failed: {entry.get('status')}")
                outputs = []
                for output in entry.get("outputs", {}).values():
                    outputs.extend(output.get("images", []))
                print(json.dumps({"prompt_id": prompt_id, "outputs": outputs}, ensure_ascii=False))
                return
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for {prompt_id}")


if __name__ == "__main__":
    main()
