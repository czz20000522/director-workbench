"""局部重绘：只重画蒙版里的区域，其余像素原样保留。

第一版不走独立 inpaint 模型（本机没有 FLUX Fill 一类的填充底模），而是用
Qwen Image 2.1 Turbo 已有的编辑能力做「潜空间加噪 + 蒙版外回贴」：

1. 原图编码成 latent，用 `SetLatentNoiseMask` 只在白区加噪；
2. 采样后解码，再用 `ImageCompositeMasked` 把结果贴回原图；
3. 蒙版先 `GrowMask(8)`，让模型在涂区外多 8px 的余量里收边。

未涂区域来自原图，不是 VAE 重建，所以背景、文字、logo 不会被改掉。
实测 1344×768：笔迹外 12px 区域与源图逐像素完全相同（平均差 0.0000）。

注意 `FeatherMask` 只按画布四边打折（见 comfy_extras/nodes_mask.py 的实现），
它 **不会** 软化蒙版内部边界；所以接缝的软硬完全由 `GrowMask` 的余量决定，
别再指望加 FeatherMask 就能羽化内部轮廓。
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from tools.qwen_image_t2i import DEFAULT_LORA_STRENGTH, LORA_STRENGTH

# 与文生图同一套分辨率上限：Qwen Image 2.1 在本机 16 GB 上按 2 MP 训练，
# 超过这个面积显存会涨得很快，而首帧本身就是 768×1344 这一档。
MAX_PIXELS = 1536 * 1344


def _check_prefix(prefix: str) -> None:
    path = PurePosixPath(prefix)
    if (not prefix or path.is_absolute() or ':' in prefix or '\\' in prefix
            or any(part in {'', '.', '..'} for part in prefix.split('/'))):
        raise ValueError('输出前缀必须位于输出目录内')


def graph(prompt: str, prefix: str, seed: int, source: Path, mask: Path,
          width: int, height: int, steps: int = 8, denoise: float = 0.75,
          lora_names: list[str] | None = None, check_files: bool = True) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('随机编号必须是有效的非负整数')
    if type(steps) is not int or not 4 <= steps <= 50:
        raise ValueError('采样步数须为 4 至 50 的整数')
    if not 0.1 <= float(denoise) <= 1.0:
        raise ValueError('改动幅度须在 0.1 至 1.0 之间')
    if any(type(value) is not int or value < 512 or value > 1536 or value % 8 for value in (width, height)):
        raise ValueError('图像宽高须为 512 至 1536 之间的 8 倍数')
    if width * height > MAX_PIXELS:
        raise ValueError('局部重绘按原生尺寸处理，请先把首帧缩到 2 百万像素以内')
    _check_prefix(prefix)
    if check_files:
        for item in (source, mask):
            if not Path(item).is_file():
                raise ValueError(f'重绘输入不可读取：{Path(item).name}')

    recipe = {
        '1': {'class_type': 'UnetLoaderGGUF', 'inputs': {'unet_name': 'Qwen-Image-2.1-Turbo-AD-Q4_K.gguf'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'qwen3vl_8b_int8_convrot.safetensors', 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_2.1_vae_bf16.safetensors'}},
        # 原图同时作为视觉参考：光靠 latent 局部信息，模型容易把服装结构画错。
        # resolution 传 0 = 参考图按自身尺寸编码（与 VAEEncode 的采样 latent 一致）。
        # 传 max(width, height) 会按面积把参考图放大（1344×768 → 1792×1024），
        # 实测输出逐像素完全相同，只是多算 44% 的参考 latent。
        '4': {'class_type': 'TextEncodeQwenImage21',
              'inputs': {'clip': ['2', 0], 'prompt': prompt, 'negative_prompt': '',
                         'resolution': 0, 'images': {'image_0': ['6', 0]},
                         'vae': ['3', 0]}},
        '5': {'class_type': 'QwenImage21Cache', 'inputs': {'model': ['1', 0], 'device': 'auto', 'dtype': 'default'}},
        '6': {'class_type': 'LoadImage', 'inputs': {'image': str(Path(source).resolve())}},
        '7': {'class_type': 'LoadImage', 'inputs': {'image': str(Path(mask).resolve())}},
        '8': {'class_type': 'ImageToMask', 'inputs': {'image': ['7', 0], 'channel': 'red'}},
        '9': {'class_type': 'GrowMask', 'inputs': {'mask': ['8', 0], 'expand': 8, 'tapered_corners': True}},
        # 这一步只在蒙版顶到画布边缘时起作用（内部轮廓不会被羽化）。
        '10': {'class_type': 'FeatherMask', 'inputs': {'mask': ['9', 0], 'left': 12, 'top': 12, 'right': 12, 'bottom': 12}},
        '11': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['6', 0], 'vae': ['3', 0]}},
        '12': {'class_type': 'SetLatentNoiseMask', 'inputs': {'samples': ['11', 0], 'mask': ['10', 0]}},
        '13': {'class_type': 'KSampler',
               'inputs': {'model': ['5', 0], 'positive': ['4', 0], 'negative': ['4', 1], 'latent_image': ['12', 0],
                          'seed': seed, 'steps': steps, 'cfg': 1.0, 'sampler_name': 'euler',
                          'scheduler': 'simple', 'denoise': float(denoise)}},
        '14': {'class_type': 'VAEDecode', 'inputs': {'samples': ['13', 0], 'vae': ['3', 0]}},
        '15': {'class_type': 'ImageCompositeMasked',
               'inputs': {'destination': ['6', 0], 'source': ['14', 0], 'x': 0, 'y': 0,
                          'resize_source': False, 'mask': ['10', 0]}},
        # 结果读取沿用关键帧的 '18' 节点约定，队列与登记逻辑不需要分叉。
        '18': {'class_type': 'SaveImage', 'inputs': {'images': ['15', 0], 'filename_prefix': prefix}},
    }
    chain = [name for name in (lora_names or []) if name]
    if chain:
        upstream = ['1', 0]
        for index, name in enumerate(chain):
            node_id = str(20 + index)
            recipe[node_id] = {
                'class_type': 'LoraLoaderModelOnly',
                'inputs': {'model': upstream, 'lora_name': name,
                           'strength_model': LORA_STRENGTH.get(name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1],
                                                               DEFAULT_LORA_STRENGTH)},
            }
            upstream = [node_id, 0]
        recipe['19'] = {'class_type': 'QwenImage21Cache',
                        'inputs': {'model': upstream, 'device': 'auto', 'dtype': 'default'}}
        recipe['13']['inputs']['model'] = ['19', 0]
    return recipe
