"""Qwen Image 2.1 + Fun ControlNet Union：参考图与姿势分开控制。

这条图用于「选一张人脸 + 选一个姿势」的首帧生成：

* 人脸/造型/服装走 ``QwenImage21UnionReferenceEncode``，决定长相和外观；
* 姿势走 ``QwenImage21UnionApply`` 的结构控制，决定骨架和镜头；
* 底模必须是 int8 safetensors（GGUF 的 sd.cpp 路径不执行 model patch，
  实测控制强度 0 与 1.5 输出完全一样）。

默认使用 int8 权重，放在 ``models/model_patches``，由原生 ``ModelPatchLoader``
读取：它按 ``gate_up`` 是否存在自动切 ``fused_mlp``，并用 ``dit_patch_operations``
保留 convrot 量化，权重体积从 7.03 GB 降到 3.52 GB，实测生成快约 19%。

注意别把"体积减半"当成"显存峰值减半"：实测 512×896 / 768×1344 / 1024×1536
三档峰值都在 15.2–15.7 GB，因为 ComfyUI 会主动把显存占满做缓存，峰值由缓存
策略决定而不是权重体积。int8 的收益是换页更少、更快。

bf16 权重仍在 ``models/controlnet``；T8 的 ``QwenImage21UnionLoader`` 把
``fused_mlp`` 写死为 False，认不了 int8 的融合结构，所以两条路不能混用。

``--input-directory`` 必须指向工作台数据根，否则 LoadImage 会拒绝绝对路径。
"""
from __future__ import annotations

import math
from pathlib import Path

from .qwen_image_t2i import DEFAULT_LORA_STRENGTH, LORA_STRENGTH, MAX_REFERENCES

# 回退到 bf16（生成慢约 19%，显存峰值基本不变）：把下面两行换成
#   UNION_PATCH = 'Qwen-Image-2.1-Fun-Controlnet-Union-ComfyUI.safetensors'
#   UNION_LOADER = 'QwenImage21UnionLoader'
# bf16 权重只保留在 models/controlnet（models/model_patches 里那份已清理），
# 回退时 tools/qwen_image_t2i.py 的 CONTROL_PATCH 也要一并改回 bf16 文件名。
UNION_PATCH = 'qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors'
UNION_LOADER = 'ModelPatchLoader'
UNION_UNET = 'qwen_image_2.1_turbo_int8_convrot.safetensors'
CONTROL_MODES = {'Depth': 'Depth', 'Lineart': 'Lineart', 'Pose': 'Pose'}


def graph(prompt: str, prefix: str, seed: int, width: int, height: int,
          reference_images: list[Path], control_image: Path,
          control_mode: str = 'Depth', control_strength: float = 1.0,
          steps: int = 8, lora_names: list[str] | None = None) -> dict:
    if not reference_images:
        raise ValueError('姿势控制至少需要一张人物参考图')
    if len(reference_images) > 3:
        raise ValueError('参考图最多 3 张（Union Reference Encode 的上限）')
    if control_mode not in CONTROL_MODES:
        raise ValueError(f'不支持的控制类型：{control_mode}')
    if any(type(value) is not int or value < 512 or value > 1536 or value % 8 for value in (width, height)):
        raise ValueError('图像宽高须为 512 至 1536 之间的 8 倍数')
    if not 0.0 <= control_strength <= 2.0:
        raise ValueError('控制强度须在 0 至 2 之间')
    if not 4 <= steps <= 50:
        raise ValueError('采样步数须为 4 至 50 的整数')

    path = Path(prefix)
    if not prefix or path.is_absolute() or ':' in prefix or '\\' in prefix or any(part in {'', '.', '..'} for part in prefix.split('/')):
        raise ValueError('输出前缀必须位于输出目录内')

    # 参考图的宽高比决定输出画幅；调用方已把它裁到目标比例，
    # 这里按面积反推 resolution，避免输出变成 1792×1024 这类越界尺寸。
    resolution = max(256, min(4096, int(round(math.sqrt(width * height) / 32) * 32)))

    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': UNION_UNET, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'qwen3vl_8b_int8_convrot.safetensors', 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_2.1_vae_bf16.safetensors'}},
        '8': {'class_type': 'KSampler', 'inputs': {
            'model': ['7', 0], 'positive': ['6', 0], 'negative': ['6', 1], 'latent_image': ['6', 2],
            'seed': seed, 'steps': steps, 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '9': {'class_type': 'VAEDecode', 'inputs': {'samples': ['8', 0], 'vae': ['3', 0]}},
        '18': {'class_type': 'SaveImage', 'inputs': {'images': ['9', 0], 'filename_prefix': prefix}},
    }
    # 节点 4 只负责把 Union 权重读成 MODEL_PATCH，两种加载器返回的都是
    # CoreModelPatcher，下游 QwenImage21UnionApply 不区分来源。
    if UNION_LOADER == 'ModelPatchLoader':
        graph['4'] = {'class_type': 'ModelPatchLoader', 'inputs': {'name': UNION_PATCH}}
    else:
        graph['4'] = {'class_type': 'QwenImage21UnionLoader', 'inputs': {'union_model': UNION_PATCH}}

    # 参考图：第一张决定画幅，后续两张只提供外观。
    reference_inputs: dict[str, list] = {}
    for index, reference in enumerate(reference_images):
        source = Path(reference)
        if not source.is_file():
            raise ValueError(f'参考图不可读取：{source.name}')
        node_id = f'ref{index}'
        graph[node_id] = {'class_type': 'LoadImage', 'inputs': {'image': str(source.resolve())}}
        if index == 0:
            reference_inputs['reference_image'] = [node_id, 0]
        else:
            reference_inputs[f'reference_image_{index + 1}'] = [node_id, 0]

    control_source = Path(control_image)
    if not control_source.is_file():
        raise ValueError(f'姿势控制图不可读取：{control_source.name}')
    graph['5'] = {'class_type': 'LoadImage', 'inputs': {'image': str(control_source.resolve())}}
    graph['6'] = {'class_type': 'QwenImage21UnionReferenceEncode', 'inputs': {
        'clip': ['2', 0], 'vae': ['3', 0], 'prompt': prompt, 'negative_prompt': '',
        'resolution': resolution, **reference_inputs}}
    graph['7'] = {'class_type': 'QwenImage21UnionApply', 'inputs': {
        'model': ['1', 0], 'union_patch': ['4', 0], 'vae': ['3', 0],
        'control_mode': control_mode, 'strength': float(control_strength),
        'start_percent': 0.0, 'end_percent': 1.0, 'control_image': ['5', 0]}}

    chain = [name for name in (lora_names or []) if name]
    upstream = ['1', 0]
    for index, name in enumerate(chain):
        node_id = str(20 + index)
        graph[node_id] = {'class_type': 'LoraLoaderModelOnly',
                          'inputs': {'model': upstream, 'lora_name': name,
                                     'strength_model': LORA_STRENGTH.get(
                                         name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1],
                                         DEFAULT_LORA_STRENGTH)}}
        upstream = [node_id, 0]
    if chain:
        graph['7']['inputs']['model'] = upstream
    return graph
