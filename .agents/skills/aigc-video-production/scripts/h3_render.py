#!/usr/bin/env python3
"""Render one MiniMax H3 shot without imposing project naming or folder rules."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


def _node_id(
    graph: dict[str, dict[str, Any]],
    class_type: str,
    *,
    preferred: str | None = None,
) -> str:
    if preferred in graph and graph[preferred].get("class_type") == class_type:
        return preferred
    matches = [key for key, node in graph.items() if node.get("class_type") == class_type]
    if len(matches) != 1:
        raise ValueError(f"需要唯一 {class_type} 节点，实际找到 {len(matches)} 个")
    return matches[0]


def _unique_node_id(graph: dict[str, dict[str, Any]], base: str) -> str:
    node_id = base
    suffix = 1
    while node_id in graph:
        suffix += 1
        node_id = f"{base}:{suffix}"
    return node_id


def _audio_vae_id(graph: dict[str, dict[str, Any]]) -> str:
    matches = []
    for node_id, node in graph.items():
        if node.get("class_type") != "VAELoader":
            continue
        vae_name = str(node.get("inputs", {}).get("vae_name", "")).lower()
        if "audio" in vae_name:
            matches.append(node_id)
    if len(matches) != 1:
        raise ValueError(f"需要唯一音频 VAE 节点，实际找到 {len(matches)} 个")
    return matches[0]


def _configure_sage_attention(
    graph: dict[str, dict[str, Any]], enabled: bool
) -> None:
    class_type = "MiniMaxH3MemoryEfficientSageAttentionPatch"
    sage_ids = [
        node_id for node_id, node in graph.items() if node.get("class_type") == class_type
    ]
    if len(sage_ids) > 1:
        raise ValueError(f"MiniMax H3 Sage Attention patch 节点超过一个: {sage_ids}")

    if not enabled and not sage_ids:
        return

    guider_id = _node_id(graph, "BasicGuider", preferred="105:16")
    scheduler_id = _node_id(graph, "BasicScheduler", preferred="105:9")
    model_consumers = (guider_id, scheduler_id)

    if sage_ids:
        sage_id = sage_ids[0]
        sage_link = [sage_id, 0]
        consumer_models = [
            graph[node_id].get("inputs", {}).get("model") for node_id in model_consumers
        ]
        if all(model == sage_link for model in consumer_models):
            source_model = graph[sage_id].get("inputs", {}).get("model")
        elif consumer_models[0] == consumer_models[1]:
            source_model = consumer_models[0]
            graph[sage_id].setdefault("inputs", {})["model"] = copy.deepcopy(source_model)
        else:
            raise ValueError(
                "BasicGuider 与 BasicScheduler 的模型连接不一致，无法安全配置 Sage Attention"
            )
    else:
        guider_model = graph[guider_id].get("inputs", {}).get("model")
        scheduler_model = graph[scheduler_id].get("inputs", {}).get("model")
        if guider_model != scheduler_model:
            raise ValueError(
                "启用 Sage Attention 前，BasicGuider 与 BasicScheduler 必须使用同一个模型"
            )
        source_model = guider_model
        sage_id = _unique_node_id(graph, "aigc:minimax-h3-sage-attention")
        graph[sage_id] = {
            "class_type": class_type,
            "inputs": {"model": copy.deepcopy(source_model)},
            "_meta": {"title": "MiniMax H3 Sage Attention"},
        }

    if not isinstance(source_model, list) or len(source_model) != 2:
        raise ValueError("MiniMax H3 Sage Attention patch 缺少有效的上游模型连接")
    if source_model == [sage_id, 0]:
        raise ValueError("MiniMax H3 Sage Attention patch 不能连接到自身")

    target_model = [sage_id, 0] if enabled else copy.deepcopy(source_model)
    for node_id in model_consumers:
        graph[node_id].setdefault("inputs", {})["model"] = copy.deepcopy(target_model)

    if not enabled:
        graph.pop(sage_id)


def _configure_te_speed(
    graph: dict[str, dict[str, Any]],
    enabled: bool,
    *,
    cache_depth: float,
    max_cache_steps: int,
) -> None:
    class_type = "TESpeedMiniMaxH3"
    te_ids = [
        node_id for node_id, node in graph.items() if node.get("class_type") == class_type
    ]
    if len(te_ids) > 1:
        raise ValueError(f"MiniMax H3 TE-Speed patch 节点超过一个: {te_ids}")

    if not enabled and not te_ids:
        return
    if not 0.0 <= cache_depth <= 0.95:
        raise ValueError("TE-Speed cache_depth 必须在 0.0 到 0.95 之间")
    if max_cache_steps < 0:
        raise ValueError("TE-Speed max_cache_steps 不能小于 0")

    guider_id = _node_id(graph, "BasicGuider", preferred="105:16")
    scheduler_id = _node_id(graph, "BasicScheduler", preferred="105:9")
    model_consumers = (guider_id, scheduler_id)

    if te_ids:
        te_id = te_ids[0]
        te_link = [te_id, 0]
        consumer_models = [
            graph[node_id].get("inputs", {}).get("model") for node_id in model_consumers
        ]
        if all(model == te_link for model in consumer_models):
            source_model = graph[te_id].get("inputs", {}).get("model")
        elif consumer_models[0] == consumer_models[1]:
            source_model = consumer_models[0]
            graph[te_id].setdefault("inputs", {})["model"] = copy.deepcopy(source_model)
        else:
            raise ValueError(
                "BasicGuider 与 BasicScheduler 的模型连接不一致，无法安全配置 TE-Speed"
            )
    else:
        guider_model = graph[guider_id].get("inputs", {}).get("model")
        scheduler_model = graph[scheduler_id].get("inputs", {}).get("model")
        if guider_model != scheduler_model:
            raise ValueError(
                "启用 TE-Speed 前，BasicGuider 与 BasicScheduler 必须使用同一个模型"
            )
        source_model = guider_model
        te_id = _unique_node_id(graph, "aigc:minimax-h3-te-speed")

    if not isinstance(source_model, list) or len(source_model) != 2:
        raise ValueError("MiniMax H3 TE-Speed patch 缺少有效的上游模型连接")
    if source_model == [te_id, 0]:
        raise ValueError("MiniMax H3 TE-Speed patch 不能连接到自身")

    if enabled:
        graph[te_id] = {
            "class_type": class_type,
            "inputs": {
                "model": copy.deepcopy(source_model),
                "processing_control_value": 0.12,
                "processing_percent_1": 0.1,
                "processing_percent_2": 0.9,
                "mcs": 2,
                "device": "auto",
                "cache_depth": float(cache_depth),
                "max_cache_steps": int(max_cache_steps),
            },
            "_meta": {"title": "MiniMax H3 TE-Speed (safe Turbo cache)"},
        }
        target_model = [te_id, 0]
    else:
        target_model = copy.deepcopy(source_model)
        graph.pop(te_id)

    for node_id in model_consumers:
        graph[node_id].setdefault("inputs", {})["model"] = copy.deepcopy(target_model)


def _replace_output_links(
    graph: dict[str, dict[str, Any]],
    source_id: str,
    target_id: str,
    *,
    exclude: set[str] | None = None,
) -> None:
    source = [source_id, 0]
    target = [target_id, 0]
    for node_id, node in graph.items():
        if exclude and node_id in exclude:
            continue
        inputs = node.get("inputs", {})
        for input_name, value in inputs.items():
            if value == source:
                inputs[input_name] = copy.deepcopy(target)


def _configure_selflift(
    graph: dict[str, dict[str, Any]],
    enabled: bool,
    *,
    seed: int,
    transition_step: int,
    lowres_scale: float,
    upscaler_model: str,
) -> None:
    class_type = "SelfLiftH3Sampler"
    selflift_ids = [
        node_id for node_id, node in graph.items() if node.get("class_type") == class_type
    ]
    if len(selflift_ids) > 1:
        raise ValueError(f"SelfLift H3 采样节点超过一个: {selflift_ids}")

    negative_ids = [
        node_id for node_id in graph if node_id.startswith("aigc:selflift-negative")
    ]
    if not enabled and not selflift_ids and not negative_ids:
        return
    if not enabled:
        if selflift_ids:
            standard_id = _node_id(
                graph, "SamplerCustomAdvanced", preferred="105:14"
            )
            selflift_id = selflift_ids[0]
            _replace_output_links(graph, selflift_id, standard_id)
            graph.pop(selflift_id)
        for node_id in negative_ids:
            graph.pop(node_id)
        sampler_id = _node_id(graph, "KSamplerSelect", preferred="105:17")
        sampler = graph[sampler_id]
        original = sampler.setdefault("_meta", {}).pop(
            "aigc_selflift_original_sampler_name", None
        )
        if original:
            sampler.setdefault("inputs", {})["sampler_name"] = original
        return

    if transition_step < 1:
        raise ValueError("SelfLift transition_step 必须大于 0")
    if not 0.25 <= lowres_scale <= 1.0:
        raise ValueError("SelfLift lowres_scale 必须在 0.25 到 1.0 之间")

    standard_id = _node_id(graph, "SamplerCustomAdvanced", preferred="105:14")
    guider_id = _node_id(graph, "BasicGuider", preferred="105:16")
    scheduler_id = _node_id(graph, "BasicScheduler", preferred="105:9")
    sampler_id = _node_id(graph, "KSamplerSelect", preferred="105:17")
    h3_id = _node_id(graph, "MiniMaxH3ImageToVideo", preferred="105:104")

    guider = graph[guider_id].get("inputs", {})
    scheduler = graph[scheduler_id].get("inputs", {})
    if guider.get("model") != scheduler.get("model"):
        raise ValueError(
            "启用 SelfLift 前，BasicGuider 与 BasicScheduler 必须使用同一个模型"
        )

    standard_inputs = graph[standard_id].get("inputs", {})
    h3_inputs = graph[h3_id].get("inputs", {})
    video_vae = h3_inputs.get("vae")
    if not isinstance(video_vae, list) or len(video_vae) != 2:
        raise ValueError("SelfLift 缺少 MiniMax H3 视频 VAE 连接")

    if selflift_ids:
        selflift_id = selflift_ids[0]
    else:
        selflift_id = _unique_node_id(graph, "aigc:minimax-h3-selflift")
    negative_id = negative_ids[0] if negative_ids else _unique_node_id(
        graph, "aigc:selflift-negative"
    )
    positive = guider.get("conditioning")
    graph[negative_id] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": copy.deepcopy(positive)},
        "_meta": {"title": "SelfLift zero conditioning"},
    }
    graph[selflift_id] = {
        "class_type": class_type,
        "inputs": {
            "model": copy.deepcopy(scheduler.get("model")),
            "positive": copy.deepcopy(positive),
            "negative": [negative_id, 0],
            "vae": copy.deepcopy(video_vae),
            "latent_image": copy.deepcopy(standard_inputs.get("latent_image")),
            "sampler": copy.deepcopy(standard_inputs.get("sampler")),
            "sigmas": copy.deepcopy(standard_inputs.get("sigmas")),
            "seed": int(seed),
            "cfg": 1.0,
            "transition_step": int(transition_step),
            "lowres_scale": float(lowres_scale),
            "rho": 0.0,
            "w_min": 0.5,
            "w_max": 1.0,
            "upscaler_model": upscaler_model,
            "highres_tiling": False,
        },
        "_meta": {"title": "MiniMax H3 SelfLift progressive sampler"},
    }
    _replace_output_links(graph, standard_id, selflift_id, exclude={selflift_id})

    sampler = graph[sampler_id]
    sampler_inputs = sampler.setdefault("inputs", {})
    sampler_meta = sampler.setdefault("_meta", {})
    if sampler_inputs.get("sampler_name") != "euler":
        sampler_meta.setdefault(
            "aigc_selflift_original_sampler_name", sampler_inputs.get("sampler_name")
        )
        sampler_inputs["sampler_name"] = "euler"


def prepare_h3_graph(
    template: dict[str, dict[str, Any]],
    *,
    prompt: str,
    first_frame: str,
    last_frame: str | None,
    duration_seconds: int,
    seed: int,
    filename_prefix: str,
    aspect_ratio: str = "16:9 (Widescreen)",
    megapixels: float = 0.4,
    turbo: bool = True,
    audio_guide: str | None = None,
    preserve_source_audio: bool = False,
    sage_attention: bool = False,
    te_speed: bool = False,
    te_cache_depth: float = 0.75,
    te_max_cache_steps: int = 1,
    selflift: bool = False,
    selflift_transition_step: int = 6,
    selflift_lowres_scale: float = 0.5,
    selflift_upscaler_model: str = "minimax_h3_latent_upscaler_3d_fp16.safetensors",
) -> dict[str, dict[str, Any]]:
    """Clone a successful H3 graph and replace all shot-specific state."""
    if duration_seconds not in (5, 10, 15):
        raise ValueError("duration_seconds 仅允许 5、10 或 15")
    graph = copy.deepcopy(template)

    h3_id = _node_id(graph, "MiniMaxH3ImageToVideo", preferred="105:104")
    h3_inputs = graph[h3_id].setdefault("inputs", {})
    h3_inputs["prompt"] = prompt

    stale_audio_nodes = {
        node_id
        for node_id in graph
        if node_id.startswith(("aigc:load-audio-guide", "aigc:add-audio-guide"))
    }
    for node_id in stale_audio_nodes:
        graph.pop(node_id)
    guider_id = None
    create_video_id = None
    create_video = None
    create_meta = None
    if stale_audio_nodes or audio_guide:
        guider_id = _node_id(graph, "BasicGuider", preferred="105:16")
        graph[guider_id].setdefault("inputs", {})["conditioning"] = [h3_id, 0]
        create_video_id = _node_id(graph, "CreateVideo", preferred="105:91")
        create_video = graph[create_video_id]
        create_meta = create_video.setdefault("_meta", {})
        original_audio_link = create_meta.pop("aigc_original_audio_link", None)
        if original_audio_link is not None:
            create_video.setdefault("inputs", {})["audio"] = original_audio_link

    if preserve_source_audio and not audio_guide:
        raise ValueError("preserve_source_audio 需要 audio_guide")

    first_link = h3_inputs.get("first_frame")
    if isinstance(first_link, list) and first_link and first_link[0] in graph:
        first_id = first_link[0]
    else:
        first_id = _node_id(graph, "LoadImage", preferred="114")
        h3_inputs["first_frame"] = [first_id, 0]
    graph[first_id].setdefault("inputs", {})["image"] = first_frame

    if last_frame:
        last_id = "ip-video:last-frame"
        suffix = 1
        while last_id in graph:
            suffix += 1
            last_id = f"ip-video:last-frame:{suffix}"
        graph[last_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": last_frame},
            "_meta": {"title": "Last Frame"},
        }
        h3_inputs["last_frame"] = [last_id, 0]
    else:
        h3_inputs.pop("last_frame", None)

    duration_id = _node_id(graph, "PrimitiveFloat", preferred="105:111")
    graph[duration_id].setdefault("inputs", {})["value"] = float(duration_seconds)

    noise_id = _node_id(graph, "RandomNoise", preferred="105:15")
    graph[noise_id].setdefault("inputs", {})["noise_seed"] = int(seed)

    turbo_id = _node_id(graph, "PrimitiveBoolean", preferred="105:126")
    graph[turbo_id].setdefault("inputs", {})["value"] = bool(turbo)

    resolution_id = _node_id(graph, "ResolutionSelector", preferred="115")
    resolution_inputs = graph[resolution_id].setdefault("inputs", {})
    resolution_inputs["aspect_ratio"] = aspect_ratio
    resolution_inputs["megapixels"] = float(megapixels)

    save_id = _node_id(graph, "SaveVideo", preferred="92")
    graph[save_id].setdefault("inputs", {})["filename_prefix"] = filename_prefix

    if audio_guide:
        assert guider_id is not None
        assert create_video_id is not None
        assert create_video is not None
        assert create_meta is not None
        load_audio_id = _unique_node_id(graph, "aigc:load-audio-guide")
        add_guide_id = _unique_node_id(graph, "aigc:add-audio-guide")
        graph[load_audio_id] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": audio_guide},
            "_meta": {"title": "Load approved dialogue guide"},
        }
        graph[add_guide_id] = {
            "class_type": "MiniMaxH3AddGuide",
            "inputs": {
                "positive": [h3_id, 0],
                "latent": [h3_id, 1],
                "audio_vae": [_audio_vae_id(graph), 0],
                "audio": [load_audio_id, 0],
                "frame_idx": 0,
            },
            "_meta": {"title": "Guide H3 with approved dialogue"},
        }
        graph[guider_id].setdefault("inputs", {})["conditioning"] = [add_guide_id, 0]
        if preserve_source_audio:
            create_meta["aigc_original_audio_link"] = copy.deepcopy(
                create_video.setdefault("inputs", {}).get("audio")
            )
            graph[create_video_id].setdefault("inputs", {})["audio"] = [load_audio_id, 0]
    # Remove a stale TE node before changing Sage, then rebuild the deterministic
    # base -> Sage -> TE chain. This also makes repeated template preparation safe.
    _configure_te_speed(
        graph,
        False,
        cache_depth=te_cache_depth,
        max_cache_steps=te_max_cache_steps,
    )
    _configure_sage_attention(graph, sage_attention)
    _configure_te_speed(
        graph,
        te_speed,
        cache_depth=te_cache_depth,
        max_cache_steps=te_max_cache_steps,
    )
    _configure_selflift(
        graph,
        selflift,
        seed=seed,
        transition_step=selflift_transition_step,
        lowres_scale=selflift_lowres_scale,
        upscaler_model=selflift_upscaler_model,
    )
    return graph


def _history_timestamp(entry: dict[str, Any]) -> int:
    messages = entry.get("status", {}).get("messages", [])
    for message in messages:
        if isinstance(message, list) and len(message) > 1 and isinstance(message[1], dict):
            value = message[1].get("timestamp")
            if isinstance(value, (int, float)):
                return int(value)
    return 0


def latest_h3_graph(history: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for prompt_id, entry in history.items():
        if entry.get("status", {}).get("status_str") != "success":
            continue
        prompt = entry.get("prompt")
        if not isinstance(prompt, list) or len(prompt) < 3 or not isinstance(prompt[2], dict):
            continue
        graph = prompt[2]
        if any(node.get("class_type") == "MiniMaxH3ImageToVideo" for node in graph.values()):
            candidates.append((_history_timestamp(entry), prompt_id, graph))
    if not candidates:
        raise ValueError("history 中没有成功的 MiniMax H3 工作流")
    _, prompt_id, graph = max(candidates, key=lambda item: item[0])
    return prompt_id, copy.deepcopy(graph)


def video_output_from_history(entry: dict[str, Any]) -> str | None:
    for output in entry.get("outputs", {}).values():
        for item in output.get("images", []):
            filename = str(item.get("filename", ""))
            if filename.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                subfolder = str(item.get("subfolder", "")).replace("\\", "/").strip("/")
                return str(PurePosixPath(subfolder, filename)) if subfolder else filename
    return None


def _http_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail}") from exc


def _validate_sage_runtime(api_url: str) -> None:
    node_id = "MiniMaxH3MemoryEfficientSageAttentionPatch"
    endpoint = api_url.rstrip("/")
    node_info = _http_json(f"{endpoint}/object_info/{node_id}")
    if node_id not in node_info:
        raise RuntimeError(f"ComfyUI 未注册 {node_id}，不能提交 Sage Attention 工作流")

    stats = _http_json(f"{endpoint}/system_stats")
    system = stats.get("system", {})
    argv = system.get("argv", [])
    if system.get("os") == "win32" and "--disable-cuda-graphs" not in argv:
        raise RuntimeError(
            "Windows Sage Attention 服务必须使用 --disable-cuda-graphs 启动"
        )


def _validate_selflift_runtime(api_url: str, upscaler_model: str) -> None:
    node_id = "SelfLiftH3Sampler"
    endpoint = api_url.rstrip("/")
    node_info = _http_json(f"{endpoint}/object_info/{node_id}")
    if node_id not in node_info:
        raise RuntimeError(f"ComfyUI 未注册 {node_id}，不能提交 SelfLift 工作流")
    try:
        choices = node_info[node_id]["input"]["required"]["upscaler_model"][0]
    except (KeyError, IndexError, TypeError):
        choices = []
    if choices and upscaler_model not in choices:
        raise RuntimeError(
            f"ComfyUI 未找到 SelfLift upscaler: {upscaler_model}"
        )


def _validate_te_speed_runtime(api_url: str) -> None:
    node_id = "TESpeedMiniMaxH3"
    endpoint = api_url.rstrip("/")
    node_info = _http_json(f"{endpoint}/object_info/{node_id}")
    if node_id not in node_info:
        raise RuntimeError(f"ComfyUI 未注册 {node_id}，不能提交 TE-Speed 工作流")
    try:
        optional_inputs = node_info[node_id]["input"]["optional"]
    except (KeyError, TypeError):
        optional_inputs = {}
    if "max_cache_steps" not in optional_inputs:
        raise RuntimeError(
            "当前 TE-Speed 节点不支持 max_cache_steps，不能安全用于 4-step Turbo"
        )


def wait_for_prompt(
    prompt_id: str,
    *,
    api_url: str,
    timeout_seconds: int,
    poll_seconds: float = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    endpoint = f"{api_url.rstrip('/')}/history/{prompt_id}"
    while time.monotonic() < deadline:
        history = _http_json(endpoint)
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {}).get("status_str")
            if status in {"success", "error"}:
                return entry
        time.sleep(poll_seconds)
    raise TimeoutError(f"等待 {prompt_id} 超过 {timeout_seconds} 秒")


def _load_template(api_url: str, template_path: Path | None) -> tuple[str, dict[str, Any]]:
    if template_path:
        return str(template_path.resolve()), json.loads(template_path.read_text(encoding="utf-8"))
    history = _http_json(f"{api_url.rstrip('/')}/history?max_items=50")
    return latest_h3_graph(history)


def submit_shot(
    *,
    api_url: str,
    template_path: Path | None,
    prompt: str,
    first_frame: str,
    last_frame: str | None,
    duration_seconds: int,
    seed: int,
    filename_prefix: str,
    aspect_ratio: str,
    megapixels: float,
    turbo: bool,
    audio_guide: str | None = None,
    preserve_source_audio: bool = False,
    sage_attention: bool = False,
    te_speed: bool = False,
    te_cache_depth: float = 0.75,
    te_max_cache_steps: int = 1,
    selflift: bool = False,
    selflift_transition_step: int = 6,
    selflift_lowres_scale: float = 0.5,
    selflift_upscaler_model: str = "minimax_h3_latent_upscaler_3d_fp16.safetensors",
) -> dict[str, Any]:
    if sage_attention:
        _validate_sage_runtime(api_url)
    if te_speed:
        _validate_te_speed_runtime(api_url)
    if selflift:
        _validate_selflift_runtime(api_url, selflift_upscaler_model)
    source_template, template = _load_template(api_url, template_path)
    graph = prepare_h3_graph(
        template,
        prompt=prompt,
        first_frame=first_frame,
        last_frame=last_frame,
        duration_seconds=duration_seconds,
        seed=seed,
        filename_prefix=filename_prefix,
        aspect_ratio=aspect_ratio,
        megapixels=megapixels,
        turbo=turbo,
        audio_guide=audio_guide,
        preserve_source_audio=preserve_source_audio,
        sage_attention=sage_attention,
        te_speed=te_speed,
        te_cache_depth=te_cache_depth,
        te_max_cache_steps=te_max_cache_steps,
        selflift=selflift,
        selflift_transition_step=selflift_transition_step,
        selflift_lowres_scale=selflift_lowres_scale,
        selflift_upscaler_model=selflift_upscaler_model,
    )
    response = _http_json(
        f"{api_url.rstrip('/')}/prompt",
        payload={"prompt": graph, "client_id": "aigc-video-production"},
        timeout=60,
    )
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 未返回 prompt_id: {response}")
    return {
        "prompt_id": prompt_id,
        "source_template": source_template,
        "seed": seed,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "audio_guide": audio_guide,
        "preserve_source_audio": preserve_source_audio,
        "sage_attention": sage_attention,
        "te_speed": te_speed,
        "te_cache_depth": te_cache_depth if te_speed else None,
        "te_max_cache_steps": te_max_cache_steps if te_speed else None,
        "selflift": selflift,
        "selflift_transition_step": selflift_transition_step if selflift else None,
        "selflift_lowres_scale": selflift_lowres_scale if selflift else None,
        "selflift_upscaler_model": selflift_upscaler_model if selflift else None,
        "filename_prefix": filename_prefix,
        "node_errors": response.get("node_errors", {}),
    }


def _add_shot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--first-frame", required=True)
    parser.add_argument("--last-frame")
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8188")
    parser.add_argument("--duration", type=int, choices=(5, 10, 15), default=5)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--aspect-ratio", default="16:9 (Widescreen)")
    parser.add_argument("--megapixels", type=float, default=0.4)
    parser.add_argument("--no-turbo", action="store_true")
    parser.add_argument("--audio-guide")
    parser.add_argument("--preserve-source-audio", action="store_true")
    parser.add_argument("--sage-attention", action="store_true")
    parser.add_argument("--te-speed", action="store_true")
    parser.add_argument("--te-cache-depth", type=float, default=0.75)
    parser.add_argument("--te-max-cache-steps", type=int, default=1)
    parser.add_argument("--selflift", action="store_true")
    parser.add_argument("--selflift-transition-step", type=int, default=6)
    parser.add_argument("--selflift-lowres-scale", type=float, default=0.5)
    parser.add_argument(
        "--selflift-upscaler",
        default="minimax_h3_latent_upscaler_3d_fp16.safetensors",
    )


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Render focused MiniMax H3 video shots")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot-workflow")
    snapshot.add_argument("output", type=Path)
    snapshot.add_argument("--api-url", default="http://127.0.0.1:8188")

    for name in ("submit", "render"):
        command = subparsers.add_parser(name)
        _add_shot_arguments(command)
        if name == "render":
            command.add_argument("--timeout", type=int, default=7200)

    wait = subparsers.add_parser("wait")
    wait.add_argument("prompt_id")
    wait.add_argument("--api-url", default="http://127.0.0.1:8188")
    wait.add_argument("--timeout", type=int, default=7200)

    args = parser.parse_args()
    try:
        if args.command == "snapshot-workflow":
            history = _http_json(f"{args.api_url.rstrip('/')}/history?max_items=50")
            source_id, template = latest_h3_graph(history)
            snapshot_graph = prepare_h3_graph(
                template,
                prompt="[SHOT_PROMPT]",
                first_frame="[FIRST_FRAME]",
                last_frame=None,
                duration_seconds=5,
                seed=0,
                filename_prefix="[OUTPUT_PREFIX]",
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(snapshot_graph, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _print_json({"source_prompt_id": source_id, "output": str(args.output.resolve())})
            return 0

        if args.command in {"submit", "render"}:
            job = submit_shot(
                api_url=args.api_url,
                template_path=args.template,
                prompt=args.prompt,
                first_frame=args.first_frame,
                last_frame=args.last_frame,
                duration_seconds=args.duration,
                seed=args.seed,
                filename_prefix=args.output_prefix,
                aspect_ratio=args.aspect_ratio,
                megapixels=args.megapixels,
                turbo=not args.no_turbo,
                audio_guide=args.audio_guide,
                preserve_source_audio=args.preserve_source_audio,
                sage_attention=args.sage_attention,
                te_speed=args.te_speed,
                te_cache_depth=args.te_cache_depth,
                te_max_cache_steps=args.te_max_cache_steps,
                selflift=args.selflift,
                selflift_transition_step=args.selflift_transition_step,
                selflift_lowres_scale=args.selflift_lowres_scale,
                selflift_upscaler_model=args.selflift_upscaler,
            )
            if args.command == "submit":
                _print_json(job)
                return 0
            entry = wait_for_prompt(
                job["prompt_id"], api_url=args.api_url, timeout_seconds=args.timeout
            )
            video = video_output_from_history(entry)
            _print_json({"job": job, "video": video, "status": entry.get("status")})
            return 0 if video else 1

        if args.command == "wait":
            entry = wait_for_prompt(
                args.prompt_id, api_url=args.api_url, timeout_seconds=args.timeout
            )
            video = video_output_from_history(entry)
            _print_json({"prompt_id": args.prompt_id, "status": entry.get("status"), "video": video})
            return 0 if video else 1
    except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
