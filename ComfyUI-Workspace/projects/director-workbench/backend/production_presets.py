"""Reviewed production recipes. Installing a recipe never runs a model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PRESET_ID = 'h3-guided'
FIRST_PRESET_ID = 'h3-first-guided'
LOCKED_PRESET_ID = 'h3-first-locked-audio'
NATIVE_LANDSCAPE_PRESET_ID = 'h3-first-native-landscape'
NATIVE_SQUARE_PRESET_ID = 'h3-first-native-square'
NATIVE_PORTRAIT_PRESET_ID = 'h3-first-native-portrait'
TEXT_LANDSCAPE_PRESET_ID = 'h3-text-native-landscape'
TEXT_SQUARE_PRESET_ID = 'h3-text-native-square'
TEXT_PORTRAIT_PRESET_ID = 'h3-text-native-portrait'
TEXT_PRESET_IDS = (TEXT_LANDSCAPE_PRESET_ID, TEXT_SQUARE_PRESET_ID, TEXT_PORTRAIT_PRESET_ID)
END_PRESET_IDS = ('h3-frames-native-landscape', 'h3-frames-native-square', 'h3-frames-native-portrait')
SQUARE_PRESET_IDS = (NATIVE_SQUARE_PRESET_ID, TEXT_SQUARE_PRESET_ID, END_PRESET_IDS[1])
LANDSCAPE_PRESET_IDS = (NATIVE_LANDSCAPE_PRESET_ID, TEXT_LANDSCAPE_PRESET_ID, END_PRESET_IDS[0])
NATIVE_PRESET_IDS = (NATIVE_LANDSCAPE_PRESET_ID, NATIVE_SQUARE_PRESET_ID, NATIVE_PORTRAIT_PRESET_ID, *TEXT_PRESET_IDS, *END_PRESET_IDS)
PRESET_IDS = (PRESET_ID, FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS)
CONTRACTS = Path(__file__).resolve().parents[1] / 'contracts'


def aspect_ratio(preset_id: str) -> str:
    title(preset_id)
    return '1:1' if preset_id in SQUARE_PRESET_IDS else '16:9' if preset_id in LANDSCAPE_PRESET_IDS else '9:16'


def title(preset_id: str = PRESET_ID) -> str:
    if preset_id not in PRESET_IDS:
        raise ValueError('制作方案不存在')
    if preset_id in TEXT_PRESET_IDS:
        shape = {TEXT_LANDSCAPE_PRESET_ID: '横屏', TEXT_SQUARE_PRESET_ID: '方形', TEXT_PORTRAIT_PRESET_ID: '竖屏'}[preset_id]
        return f'纯文本 + H3 原生声音{shape}样片（待小样验证）'
    if preset_id in END_PRESET_IDS:
        shape = ('横屏', '方形', '竖屏')[END_PRESET_IDS.index(preset_id)]
        return f'首尾帧 + H3 原生声音{shape}样片（待小样验证）'
    if preset_id == NATIVE_PORTRAIT_PRESET_ID:
        return '首帧 + H3 原生声音竖屏样片（待小样验证）'
    if preset_id == LOCKED_PRESET_ID:
        return '首帧 + 锁定对白视频（待小样验证）'
    if preset_id == NATIVE_LANDSCAPE_PRESET_ID:
        return '首帧 + H3 原生声音横屏样片'
    if preset_id == NATIVE_SQUARE_PRESET_ID:
        return '首帧 + H3 原生声音方形样片'
    return '首帧 + 音频驱动视频' if preset_id == FIRST_PRESET_ID else '首尾帧 + 音频驱动视频'


def stage_id(preset_id: str = PRESET_ID) -> str:
    title(preset_id)
    if preset_id in (*TEXT_PRESET_IDS, *END_PRESET_IDS, NATIVE_PORTRAIT_PRESET_ID):
        return preset_id.replace('h3-', 'motion-') + '-generation'
    if preset_id == LOCKED_PRESET_ID:
        return 'motion-first-locked-audio-generation'
    if preset_id == NATIVE_LANDSCAPE_PRESET_ID:
        return 'motion-first-native-landscape-generation'
    if preset_id == NATIVE_SQUARE_PRESET_ID:
        return 'motion-first-native-square-generation'
    return 'motion-first-generation' if preset_id == FIRST_PRESET_ID else 'motion-generation'


preset_stage_id = stage_id


def load_recipe(preset_id: str = PRESET_ID) -> dict[str, Any]:
    title(preset_id)
    graph = json.loads((CONTRACTS / 'h3-guided-api.json').read_text(encoding='utf-8'))
    if preset_id in (FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS) and preset_id not in END_PRESET_IDS:
        graph.pop('ip-video:last-frame:2')
        for node in graph.values():
            node['inputs'].pop('last_frame', None)
    if preset_id in NATIVE_PRESET_IDS:
        graph.pop('aigc:add-audio-guide')
        graph.pop('aigc:load-audio-guide')
        graph['105:16']['inputs']['conditioning'] = ['105:104', 0]
        graph['115']['inputs'].update(aspect_ratio={'1:1': '1:1 (Square)', '16:9': '16:9 (Widescreen)', '9:16': '9:16 (Portrait Widescreen)'}[aspect_ratio(preset_id)], megapixels=0.4)
    if preset_id in TEXT_PRESET_IDS:
        graph.pop('114')
        graph['105:104']['inputs'].pop('first_frame')
    if preset_id == LOCKED_PRESET_ID:
        conditioning = graph['105:104']
        conditioning['class_type'] = 'MiniMaxH3AudioConditioningT8'
        values = conditioning['inputs']
        values['video_vae'] = values.pop('vae')
        values.update(
            audio_vae=['105:24', 0], drive_audio=['aigc:load-audio-guide', 0],
            task_type='I2VA', audio_mode='lock_source', audio_denoise_strength=0.0,
            add_source_as_reference=False, prompt_primary_audio_ordinal=0,
            strict_prompt_tags=True, ref_image_size='match',
            reference_video_policy='official_2_to_15s',
        )
        graph.pop('aigc:add-audio-guide')
        graph.pop('105:23')
        graph['105:16']['inputs']['conditioning'] = ['105:104', 0]
        # Output 1 carries the source audio latent and its zero noise mask.
        # Output 2 is the original PCM guide, not a VAE reconstruction.
        graph['aigc:trim-locked-output'] = {
            'class_type': 'MiniMaxH3OutputTrimT8',
            'inputs': {'frames': ['105:10', 0], 'audio': ['105:104', 2],
                       'start_seconds': 0.0, 'duration_seconds': ['105:111', 0], 'fps': 24.0},
        }
        graph['105:91']['inputs'].update(
            images=['aigc:trim-locked-output', 0], audio=['aigc:trim-locked-output', 1])
    return graph


def input_specs(preset_id: str = PRESET_ID) -> list[dict[str, Any]]:
    title(preset_id)
    document = json.loads((CONTRACTS / 'pipeline-inputs.json').read_text(encoding='utf-8'))
    specs = document['stages']['motion-generation']['inputs']
    if preset_id in (FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS) and preset_id not in END_PRESET_IDS:
        specs = [spec for spec in specs if spec['id'] != 'last-frame']
    if preset_id in NATIVE_PRESET_IDS:
        specs = [spec for spec in specs if spec['id'] != 'guide']
    if preset_id in TEXT_PRESET_IDS:
        specs = [spec for spec in specs if spec['id'] != 'first-frame']
    for spec in specs:
        if spec['id'] == 'duration':
            spec.update(min=4, max=15, step=0.1,
                        description='H3 支持 4–15 秒；实际帧数按 24 fps 和 17k+5 网格对齐。分镜时长需一致。')
        if spec['id'] == 'seed':
            spec['example'] = '42'
        if spec['id'] == 'guide' and preset_id == LOCKED_PRESET_ID:
            spec['description'] = '已定稿对白时间轴：同时锁定生成音频 latent 并作为交付音轨；请包含镜头内的静音和停顿，音乐后期添加。'
    return specs


def catalog(model_root: Path, preset_id: str = PRESET_ID) -> dict[str, Any]:
    label = title(preset_id)
    try:
        graph = load_recipe(preset_id)
        inputs = input_specs(preset_id)
        for spec in inputs:
            binding = spec['binding']
            graph[binding['node_id']]['inputs'][binding['input']]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'id': preset_id, 'title': label, 'available': False, 'reason': f'制作方案文件不完整：{exc}', 'missing_models': []}
    categories = {'unet_name': ('diffusion_models', 'unet'), 'clip_name': ('text_encoders', 'clip'), 'vae_name': ('vae',), 'lora_name': ('loras',)}
    missing = []
    for node in graph.values():
        for key, directories in categories.items():
            name = node['inputs'].get(key)
            if name and not any(any((model_root / directory).rglob(name)) for directory in directories):
                missing.append(name)
    return {
        'id': preset_id, 'title': label, 'available': True,
        'description': (f'H3 · 首尾帧 · {aspect_ratio(preset_id)} · 0.4MP · 4–15 秒；原生声音待小样验证。'
                        if preset_id in END_PRESET_IDS else
                        f'H3 · 纯文本 · {aspect_ratio(preset_id)} · 0.4MP · 4–15 秒。只需提示词与参数，不需图片或外部音频；本机音画 latent 路径，原生声音待真实小样验收。'
                        if preset_id in TEXT_PRESET_IDS else
                        'H3 · 竖屏9:16 · 0.4MP · 4–15秒；首帧与提示词，原生声音待小样验证。'
                        if preset_id == NATIVE_PORTRAIT_PRESET_ID else
                        'H3 · 横屏 16:9 · 约 0.4MP · 4–15 秒。首帧、提示词，声音由 H3 原生生成；与官方 1344×768 高分辨率设置不同。'
                        if preset_id == NATIVE_LANDSCAPE_PRESET_ID else
                        'H3 · 方形 1:1 · 0.4MP（640×640）· 4–15 秒。首帧、提示词，声音由 H3 原生生成。'
                        if preset_id == NATIVE_SQUARE_PRESET_ID else
                        'H3 · 首帧 · 4–15 秒。锁定对白 latent，原始对白用于交付；口型及无字幕画面仍需小样验收。'
                        if preset_id == LOCKED_PRESET_ID else
                        'H3 · 竖屏 · 约 0.4MP · 4–15 秒。首帧和表演音频引导，不约束结束姿态，适合转身、行走等动作。'
                        if preset_id == FIRST_PRESET_ID else 'H3 · 竖屏 · 约 0.4MP · 4–15 秒。准备首帧、尾帧、表演音频和提示词后生成样片。'),
        'verification': (
                         '已验证图结构与输入契约；未提交 GPU 小样，fp16 VAE 的显存与画质需实测。'
                         if preset_id in NATIVE_PRESET_IDS else
                         '已验证连接和输入契约，尚未完成模型小样；不保证口型或无字幕。' if preset_id == LOCKED_PRESET_ID
                         else '沿用已有作品验证过的执行连接；新作品仍需生成小样并审核。'),
        'missing_models': sorted(set(missing)),
    }


def stage(workflow: str, order: int, preset_id: str = PRESET_ID) -> dict[str, Any]:
    first_only = preset_id in (FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS)
    native_audio = preset_id in NATIVE_PRESET_IDS
    text_only = preset_id in TEXT_PRESET_IDS
    shape = {'16:9': '横屏', '1:1': '方形', '9:16': '竖屏'}[aspect_ratio(preset_id)]
    frames = '首尾帧' if preset_id in END_PRESET_IDS else '首帧'
    return {
        'id': stage_id(preset_id), 'order': order, 'title': title(preset_id),
        'purpose': '仅用本作提示词生成音画样片，不需上传首帧或外部音频；原生声音仍待小样验证。' if text_only else f'将本作{frames}和提示词生成带原生声音的{shape}视频样片。' if native_audio else '将本作首帧、提示词和表演引导音频生成视频样片。' if first_only else '将本作首尾帧、提示词和表演引导音频生成视频样片。',
        'backend': 'ComfyUI 工作流', 'status': '已接入', 'recipe': 'H3 纯文本与原生声音' if text_only else f'H3 {frames}与原生声音' if native_audio else 'H3 首帧与音频引导' if first_only else 'H3 首尾帧与音频引导',
        'generation_mode': 'text_to_video' if text_only else 'image_to_video',
        'inputs': ['提示词'] if text_only else ['首帧', '尾帧', '提示词'] if preset_id in END_PRESET_IDS else ['首帧', '提示词'] if native_audio else ['首帧', '表演引导音频', '提示词'] if first_only else ['首帧', '尾帧', '表演引导音频', '提示词'], 'outputs': ['视频候选'],
        'note': '制作方案已配置；提交仍需检查素材、模型和 ComfyUI 连接。',
        'input_specs': input_specs(preset_id),
        'execution': {'mode': 'comfyui', 'label': 'H3 原生音画样片' if native_audio else 'H3 音频驱动样片', 'references': [workflow],
                      'state': '待小样验证' if preset_id in (LOCKED_PRESET_ID, NATIVE_SQUARE_PRESET_ID, NATIVE_PORTRAIT_PRESET_ID, *TEXT_PRESET_IDS, *END_PRESET_IDS) else '已验证'},
    }
