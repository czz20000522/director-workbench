"""Krea 2 Turbo text-only graph using installed ComfyUI core nodes.

Based on Comfy-Org/workflow_templates/templates/image_krea2_turbo_t2i.json:
8-step Euler/simple, CFG 1, zeroed negative conditioning. Optional prompt
enhancement and style LoRA are disabled; the user's prompt is used verbatim.
"""


def graph(prompt: str, prefix: str, seed: int, width: int = 768, height: int = 1344) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('种子必须是有效的非负整数')
    if any(type(value) is not int or value < 512 or value > 1536 or value % 64 for value in (width, height)):
        raise ValueError('图像宽高须为 512 至 1536 之间的 64 倍数')
    if width * height > 1536 * 1024:
        raise ValueError('首版文字生图限制为约 1.5 百万像素')
    if not prefix or ':' in prefix or '\\' in prefix or any(part in {'', '.', '..'} for part in prefix.split('/')):
        raise ValueError('输出前缀必须位于输出目录内')
    return {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'krea2_turbo_fp8_scaled.safetensors', 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'qwen3vl_4b_fp8_scaled.safetensors', 'type': 'krea2', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_vae.safetensors'}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'clip': ['2', 0], 'text': prompt}},
        '5': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['4', 0]}},
        '6': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '7': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'positive': ['4', 0], 'negative': ['5', 0],
            'latent_image': ['6', 0], 'seed': seed, 'steps': 8, 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['3', 0]}},
        '18': {'class_type': 'SaveImage', 'inputs': {'images': ['8', 0], 'filename_prefix': prefix}},
    }
