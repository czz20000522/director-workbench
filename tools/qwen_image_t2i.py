"""Qwen Image 2.1 text-to-image graph using installed ComfyUI core nodes.

The graph follows the official Comfy-Org Qwen Image 2.1 T2I template but keeps
the prompt enhancer disabled for the first workbench baseline. This avoids a
second large text model.

Deck profile (RTX 5070 Ti 16GB / cu130): the DiT loads through ComfyUI-GGUF
at Q4_K. 8 steps is the Turbo distillation's native working point, not a VRAM
compromise -- more steps measurably degrade the image (see 使用说明.md).
Video-side resolution/steps are NOT set here; they come from backend/hardware.py.

`references` feeds the same graph up to three reference images. The text encoder
encodes them twice — as vision tokens and as VAE reference latents — which is how
this model composes 人像 + 服装 + 姿势 without the separate Edit checkpoint.
"""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath

MAX_REFERENCES = 5

# Qwen-Image-2.1 Fun ControlNet Union：一份权重覆盖 depth / pose / lineart 等
# 结构条件。放在 `models/model_patches`，由 ModelPatchLoader 读取。
# 用 int8 版：ModelPatchLoader 会按 gate_up 切 fused_mlp 并保留 convrot 量化。
# 权重 3.52 GB（bf16 是 7.03 GB），实测生成快约 19%；显存峰值基本不变，
# 因为 ComfyUI 会主动把显存占满做缓存。models/model_patches 里的 bf16 副本已清理。
CONTROL_PATCH = 'qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors'
CONTROL_STRENGTH = 0.9
# GGUF(UnetLoaderGGUF) 走 sd.cpp 兼容模式，ComfyUI 的 model patch 不参与推理，
# 实测控制强度 0 与 1.5 输出完全相同。带姿势控制时换成官方 int8 底模，
# 补丁才会真正挂上；普通生图仍用 GGUF，速度不受影响。
CONTROL_UNET = 'qwen_image_2.1_turbo_int8_convrot.safetensors'

# 每条 LoRA 的强度单独给：主 LoRA 决定画风/角色走向，去审查 LoRA 只做减法，
# 叠满会把脸和材质一起推糊，所以默认轻一点。
LORA_STRENGTH = {'qwen-image-2.1-uncensored-lora.safetensors': 0.7}
DEFAULT_LORA_STRENGTH = 0.85


def graph(prompt: str, prefix: str, seed: int, width: int = 1024, height: int = 1024,
          lora_name: str | None = None, steps: int = 8,
          references: list[Path] | None = None,
          lora_names: list[str] | None = None,
          refine: bool = False, refine_strength: float = 0.55,
          control_image: Path | None = None,
          control_strength: float = CONTROL_STRENGTH,
          control_patch: str = CONTROL_PATCH) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('随机编号必须是有效的非负整数')
    # 8 是 Turbo 蒸馏的标称步数；更高的值由调用方显式给出，这里不替它挑选。
    if type(steps) is not int or not 4 <= steps <= 50:
        raise ValueError('采样步数须为 4 至 50 的整数')
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
        '7': {'class_type': 'KSampler', 'inputs': {'model': ['5', 0], 'positive': ['4', 0], 'negative': ['4', 1], 'latent_image': ['6', 0], 'seed': seed, 'steps': steps, 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['3', 0]}},
        '18': {'class_type': 'SaveImage', 'inputs': {'images': ['8', 0], 'filename_prefix': prefix}},
    }
    if control_image is not None:
        graph['1'] = {'class_type': 'UNETLoader',
                      'inputs': {'unet_name': CONTROL_UNET, 'weight_dtype': 'default'}}
    if references:
        # Autogrow 输入按 image_0/image_1… 的键排序，键名不能改。
        if len(references) > MAX_REFERENCES:
            raise ValueError(f'参考图最多 {MAX_REFERENCES} 张')
        images: dict[str, list] = {}
        for index, reference in enumerate(references):
            path = Path(reference)
            if not path.is_file():
                raise ValueError(f'参考图不可读取：{path.name}')
            node_id = f'ref{index}'
            graph[node_id] = {'class_type': 'LoadImage', 'inputs': {'image': str(path.resolve())}}
            images[f'image_{index}'] = [node_id, 0]
        graph['4']['inputs']['images'] = images
    chain = [name for name in ([lora_name] if lora_name else []) + list(lora_names or []) if name]
    upstream = ['1', 0]
    if chain:
        # LoRA 必须串在 loader 和 cache 之间：早先直接覆盖节点 '5' 会把
        # QwenImage21Cache 删掉，采样器拿到的是一个没有 KV-cache 摆放的模型。
        # 多条 LoRA 依次串接，最后一条再接到 cache。
        for index, name in enumerate(chain):
            node_id = str(20 + index)
            graph[node_id] = {'class_type': 'LoraLoaderModelOnly',
                              'inputs': {'model': upstream,
                                         'lora_name': name,
                                         'strength_model': LORA_STRENGTH.get(name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1],
                                                                             DEFAULT_LORA_STRENGTH)}}
            upstream = [node_id, 0]
    if control_image is not None:
        # 姿势/结构控制走真正的 ControlNet，而不是把深度图混进多参考：
        # 多参考只会把它当又一张素材，ControlNet 才会把它当骨架。
        control_source = Path(control_image)
        if not control_source.is_file():
            raise ValueError(f'控制图不可读取：{control_source.name}')
        if not 0.0 <= control_strength <= 2.0:
            raise ValueError('控制强度须在 0 至 2 之间')
        graph['30'] = {'class_type': 'ModelPatchLoader', 'inputs': {'name': control_patch}}
        graph['31'] = {'class_type': 'LoadImage', 'inputs': {'image': str(control_source.resolve())}}
        graph['32'] = {'class_type': 'QwenImageDiffsynthControlnet',
                       'inputs': {'model': upstream, 'model_patch': ['30', 0], 'vae': ['3', 0],
                                  'image': ['31', 0], 'strength': float(control_strength)}}
        upstream = ['32', 0]
    if chain or control_image is not None:
        graph['19'] = {'class_type': 'QwenImage21Cache', 'inputs': {'model': upstream, 'device': 'auto', 'dtype': 'default'}}
        graph['7']['inputs']['model'] = ['19', 0]
    if refine:
        # 降噪走图像空间，而不是二次采样。实测低 denoise（0.2/0.3）再采样会让
        # Turbo 在原图上「补细节」，高频能量反而 +100%/+19%，越降越糙。
        # 这里用「轻度高斯 + 按比例混回原图」：结构不动，只压高频颗粒，
        # 耗时是毫秒级，不增加采样步数。
        if not 0.1 <= refine_strength <= 0.9:
            raise ValueError('降噪强度须在 0.1 至 0.9 之间')
        graph['40'] = {'class_type': 'ImageBlur',
                       'inputs': {'image': ['8', 0], 'blur_radius': 1, 'sigma': 1.0}}
        graph['41'] = {'class_type': 'ImageBlend',
                       'inputs': {'image1': ['8', 0], 'image2': ['40', 0],
                                  # blend_factor 是原图的权重：0.55 = 55% 原图 + 45% 轻模糊。
                                  'blend_factor': refine_strength, 'blend_mode': 'normal'}}
        graph['18']['inputs']['images'] = ['41', 0]
    return graph
