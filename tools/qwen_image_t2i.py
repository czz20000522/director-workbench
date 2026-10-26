"""Qwen Image 2.1 text-to-image graph using installed ComfyUI core nodes.

The graph follows the official Comfy-Org Qwen Image 2.1 T2I template but keeps
the prompt enhancer disabled for the first workbench baseline. This avoids a
second large text model.

Deck profile for this machine (RTX 4060 Ti 8GB): the DiT loads through
ComfyUI-GGUF at Q4_K_M instead of the int8_convrot checkpoint, because a 6.8 GiB
single-file weight does not fit next to the KV cache and activations on an 8GB
card. The Turbo distillation replaces the 25-step sampler with 8 steps, the same
trade the video presets already make.
"""
from __future__ import annotations

from pathlib import PurePosixPath


def graph(prompt: str, prefix: str, seed: int, width: int = 1024, height: int = 1024,
          lora_name: str | None = None) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('随机编号必须是有效的非负整数')
    if any(type(value) is not int or value < 512 or value > 1536 or value % 8 for value in (width, height)):
        raise ValueError('图像宽高须为 512 至 1536 之间的 8 倍数')
    if width * height > 1536 * 1344:
        raise ValueError('首版文字生图限制为约 2.0 百万像素')
    path = PurePosixPath(prefix)
    if not prefix or path.is_absolute() or ':' in prefix or '\\' in prefix or any(part in {'', '.', '..'} for part in prefix.split('/')):
        raise ValueError('输出前缀必须位于输出目录内')
    graph = {
        '1': {'class_type': 'UnetLoaderGGUF', 'inputs': {'unet_name': 'Qwen-Image-2.1-Turbo-AD-Q4_K.gguf'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'qwen3vl_8b_int8_convrot.safetensors', 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_2.1_vae_bf16.safetensors'}},
        '4': {'class_type': 'TextEncodeQwenImage21', 'inputs': {'clip': ['2', 0], 'prompt': prompt, 'negative_prompt': '', 'resolution': max(width, height), 'images': [], 'vae': ['3', 0]}},
        '5': {'class_type': 'QwenImage21Cache', 'inputs': {'model': ['1', 0], 'device': 'auto', 'dtype': 'default'}},
        '6': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '7': {'class_type': 'KSampler', 'inputs': {'model': ['5', 0], 'positive': ['4', 0], 'negative': ['4', 1], 'latent_image': ['6', 0], 'seed': seed, 'steps': 8, 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['3', 0]}},
        '18': {'class_type': 'SaveImage', 'inputs': {'images': ['8', 0], 'filename_prefix': prefix}},
    }
    if lora_name:
        # The LoRA has to sit between the loader and the cache: overwriting node '5'
        # here used to delete the QwenImage21Cache node and hand the sampler a model
        # with no KV-cache placement at all.
        graph['5'] = {'class_type': 'LoraLoaderModelOnly', 'inputs': {'model': ['1', 0], 'lora_name': lora_name, 'strength_model': 0.85}}
        graph['19'] = {'class_type': 'QwenImage21Cache', 'inputs': {'model': ['5', 0], 'device': 'auto', 'dtype': 'default'}}
        graph['7']['inputs']['model'] = ['19', 0]
    return graph
