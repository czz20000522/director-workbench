"""Reviewed production recipes. Installing a recipe never runs a model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import hardware

PRESET_ID = 'h3-guided'
FIRST_PRESET_ID = 'h3-first-guided'
LOCKED_PRESET_ID = 'h3-first-locked-audio'
NATIVE_LANDSCAPE_PRESET_ID = 'h3-first-native-landscape'
NATIVE_SQUARE_PRESET_ID = 'h3-first-native-square'
NATIVE_PORTRAIT_PRESET_ID = 'h3-first-native-portrait'
TEXT_LANDSCAPE_PRESET_ID = 'h3-text-native-landscape'
TEXT_SQUARE_PRESET_ID = 'h3-text-native-square'
TEXT_PORTRAIT_PRESET_ID = 'h3-text-native-portrait'
FAST_TEXT_LANDSCAPE_PRESET_ID = 'h3-text-fast-v4-kitchen'
FAST_IMAGE_LANDSCAPE_PRESET_ID = 'h3-first-fast-kitchen'
ADULT_TRIGGER = 'r34l1sm'

ADULT_REALISM_TEXT_PRESET_ID = 'h3-text-realism-people'
ADULT_REALISM_IMAGE_PRESET_ID = 'h3-first-realism-people'
QUALITY_IMAGE_LANDSCAPE_PRESET_ID = 'h3-first-quality-landscape'
# Pika H3 Flex：官方 6 步 LCM 联合音视频，LoRA 挂在 H3 底模上。
# 目前只接纯文字横屏；FL2VA / Ref2VA 等素材链路验证后再加。
PIKA_TEXT_LANDSCAPE_PRESET_ID = 'h3-pika-text-landscape'
PIKA_PRESET_IDS = (PIKA_TEXT_LANDSCAPE_PRESET_ID,)
ADULT_PRESET_IDS = (ADULT_REALISM_TEXT_PRESET_ID, ADULT_REALISM_IMAGE_PRESET_ID)
QUALITY_PRESET_IDS = (QUALITY_IMAGE_LANDSCAPE_PRESET_ID,)
TEXT_PRESET_IDS = (TEXT_LANDSCAPE_PRESET_ID, TEXT_SQUARE_PRESET_ID, TEXT_PORTRAIT_PRESET_ID, ADULT_REALISM_TEXT_PRESET_ID)
# 分辨率 / 步数 / 预留显存一律读 backend/hardware.py 的档位，这里不再写死。
# 旧配置（RTX 4060 Ti 8GB：全档 0.4MP、8 步、reserve-vram 1.0）已作废，
# 只作为 <10 GiB 机器的 compact 兜底保留在 hardware.py 里。
END_PRESET_IDS = ('h3-frames-native-landscape', 'h3-frames-native-square', 'h3-frames-native-portrait')
SQUARE_PRESET_IDS = (NATIVE_SQUARE_PRESET_ID, TEXT_SQUARE_PRESET_ID, END_PRESET_IDS[1])
LANDSCAPE_PRESET_IDS = (NATIVE_LANDSCAPE_PRESET_ID, TEXT_LANDSCAPE_PRESET_ID, ADULT_REALISM_TEXT_PRESET_ID,
                        QUALITY_IMAGE_LANDSCAPE_PRESET_ID, END_PRESET_IDS[0], *PIKA_PRESET_IDS)
TEXT_PRESET_IDS = (*TEXT_PRESET_IDS, FAST_TEXT_LANDSCAPE_PRESET_ID)
NATIVE_PRESET_IDS = (NATIVE_LANDSCAPE_PRESET_ID, NATIVE_SQUARE_PRESET_ID, NATIVE_PORTRAIT_PRESET_ID,
                     FAST_IMAGE_LANDSCAPE_PRESET_ID, ADULT_REALISM_IMAGE_PRESET_ID,
                     QUALITY_IMAGE_LANDSCAPE_PRESET_ID, *TEXT_PRESET_IDS, *END_PRESET_IDS)
PRESET_IDS = (PRESET_ID, FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS, *PIKA_PRESET_IDS)
CONTRACTS = Path(__file__).resolve().parents[1] / 'contracts'


def aspect_ratio(preset_id: str) -> str:
    title(preset_id)
    return '1:1' if preset_id in SQUARE_PRESET_IDS else '16:9' if preset_id in (*LANDSCAPE_PRESET_IDS, FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID, ADULT_REALISM_IMAGE_PRESET_ID) else '9:16'


def megapixels(preset_id: str) -> float:
    title(preset_id)
    if preset_id in PIKA_PRESET_IDS:
        # Pika 官方权重只到 480p/768p；这里给 768p（16:9 约 1024×576）。
        return 0.6
    if preset_id in (FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID):
        return hardware.video_megapixels('fast')
    if preset_id in ADULT_PRESET_IDS or preset_id in QUALITY_PRESET_IDS:
        return hardware.video_megapixels('quality')
    return hardware.video_megapixels('standard')


def turbo_default(preset_id: str) -> bool:
    title(preset_id)
    # Pika 是固定 6 步 LCM，没有 Turbo / 精细两条采样分支。
    if preset_id in PIKA_PRESET_IDS:
        return False
    # False = 关闭 Turbo，走 105:123 的 20 步分支（精细档 / 成人向）。
    if preset_id in QUALITY_PRESET_IDS or preset_id in ADULT_PRESET_IDS:
        return False
    return True


def _steps_for(preset_id: str, graph: dict[str, Any]) -> int | None:
    """界面要显示的步数：跟着 Turbo 开关走的那一侧。

    `105:9.steps` 接的是 `105:123` 开关，两侧分别是 `105:124`（20 步）和
    `105:125`（8 步），所以不能直接读 KSampler 的 steps 字段。
    """
    if preset_id in PIKA_PRESET_IDS:
        return 6
    switch = graph.get('105:123', {}).get('inputs') or {}
    turbo = turbo_default(preset_id)

    def value_at(link: Any) -> int | None:
        if isinstance(link, list) and len(link) == 2:
            node = graph.get(str(link[0])) or {}
            raw = (node.get('inputs') or {}).get('value')
            return raw if type(raw) is int else None
        return None

    return value_at(switch.get('on_true')) if turbo else value_at(switch.get('on_false'))


def title(preset_id: str = PRESET_ID) -> str:
    if preset_id not in PRESET_IDS:
        raise ValueError('制作方案不存在')
    return plain_title(preset_id)


def plain_title(preset_id: str) -> str:
    """界面用的白话名：只说「要准备什么」和「出来什么画幅」，不写模型与算子名。

    旧标题里的 `0.4MP + Kitchen`、`H3 原生声音样片（待小样验证）` 是内部验收用语，
    在单步工具里只会让人不知道该选哪个。技术细节仍留在 description/verification。
    """
    if preset_id in PIKA_PRESET_IDS:
        return 'Pika 6 步 · 纯文字 · 横屏 16:9'
    if preset_id in END_PRESET_IDS:
        names = ('横屏 16:9', '方形 1:1', '竖屏 9:16')
        return f'首尾帧衔接 · {names[END_PRESET_IDS.index(preset_id)]}'
    if preset_id == TEXT_LANDSCAPE_PRESET_ID:
        return '纯文字生成 · 横屏 16:9'
    if preset_id == TEXT_SQUARE_PRESET_ID:
        return '纯文字生成 · 方形 1:1'
    if preset_id == TEXT_PORTRAIT_PRESET_ID:
        return '纯文字生成 · 竖屏 9:16'
    if preset_id == FAST_TEXT_LANDSCAPE_PRESET_ID:
        return '快速纯文字生成 · 横屏 16:9'
    if preset_id == ADULT_REALISM_TEXT_PRESET_ID:
        return '真人质感（成人向）· 纯文字 · 横屏 16:9'
    if preset_id == FAST_IMAGE_LANDSCAPE_PRESET_ID:
        return '快速出片 · 横屏 16:9'
    if preset_id == ADULT_REALISM_IMAGE_PRESET_ID:
        return '真人质感（成人向）· 图生视频 · 横屏 16:9'
    if preset_id in QUALITY_PRESET_IDS:
        return '长采样对照 · 横屏 16:9'
    if preset_id == NATIVE_LANDSCAPE_PRESET_ID:
        return '标准出片 · 横屏 16:9'
    if preset_id == NATIVE_SQUARE_PRESET_ID:
        return '标准出片 · 方形 1:1'
    if preset_id == NATIVE_PORTRAIT_PRESET_ID:
        return '标准出片 · 竖屏 9:16'
    if preset_id == LOCKED_PRESET_ID:
        return '锁定对白口型 · 竖屏 9:16'
    if preset_id == FIRST_PRESET_ID:
        return '表演音频驱动动作 · 竖屏 9:16'
    return '首尾帧 + 表演音频 · 竖屏 9:16'


# 同组里的档位差异。写具体差别，不要复述组说明。
_PLAIN_SUMMARY = {
    NATIVE_LANDSCAPE_PRESET_ID: '适合横屏成片；画面与速度比较均衡，声音由模型自己生成。',
    NATIVE_SQUARE_PRESET_ID: '适合方形社媒画面，构图更集中；声音由模型自己生成。',
    NATIVE_PORTRAIT_PRESET_ID: '适合手机竖屏；声音由模型自己生成。',
    FAST_IMAGE_LANDSCAPE_PRESET_ID: '本机实测最快：同一片段耗时约为标准档的三分之二；用 8 步采样换速度，细节略少于标准档。',
    ADULT_REALISM_IMAGE_PRESET_ID: '多挂一层真人质感 LoRA，人物更写实；仅限虚构成年人内容。',
    QUALITY_IMAGE_LANDSCAPE_PRESET_ID: (
        '20 步完整采样。11-07 实测：画质与 8 步标准档没有可见差别，只换了一套动作结果，'
        '耗时却从 211 秒涨到 381 秒。仅用于对照，日常出片请用标准档。'),
    TEXT_LANDSCAPE_PRESET_ID: '不给图，模型按文字自己构图，横屏输出。',
    TEXT_SQUARE_PRESET_ID: '不给图，模型按文字自己构图，方形输出。',
    TEXT_PORTRAIT_PRESET_ID: '不给图，模型按文字自己构图，竖屏输出。',
    FAST_TEXT_LANDSCAPE_PRESET_ID: '本机实测最快；8 步采样 + 加速注意力，适合先出小样看构图。',
    ADULT_REALISM_TEXT_PRESET_ID: '多挂一层真人质感 LoRA，人物更写实；仅限虚构成年人内容。',
    LOCKED_PRESET_ID: '对白时间轴会锁进视频，人物按这段声音开口；口型仍需小样确认。',
    FIRST_PRESET_ID: '用一段声音引导人物怎么动，不限制结束姿态，适合转身、行走。',
    PRESET_ID: '两张图之外再加一段声音，动作起点、终点和节奏都定下来。',
    PIKA_TEXT_LANDSCAPE_PRESET_ID: 'Pika 官方 6 步 LCM：一次出画面和声音，交付 5.0 秒（生成 124 帧、交付 120 帧）；原生 768p，不叠 Turbo。',
}


def guide(preset_id: str) -> dict[str, Any]:
    """给方案选择器用的结构化白话信息。前端不该再靠 id 猜语义。"""
    if preset_id in PIKA_PRESET_IDS:
        group, needs = 'text', ['一句画面描述']
        summary = 'Pika 6 步 LCM，一次生成画面和声音；交付 5.0 秒、原生 768p。'
    elif preset_id in TEXT_PRESET_IDS:
        group, needs = 'text', ['一句画面描述']
        summary = '不用准备图片，写好画面与动作就能生成一段视频。'
    elif preset_id in END_PRESET_IDS or preset_id == PRESET_ID:
        group, needs = 'frames', ['开始画面', '结束画面', '一句动作描述']
        summary = '给两张图，模型补出中间的运动；适合控制「从哪里动到哪里」。'
        if preset_id == PRESET_ID:
            needs.append('一段表演音频')
            summary = '两张图之外再加一段声音，动作起点、终点和节奏都定下来。'
    elif preset_id in (FIRST_PRESET_ID, LOCKED_PRESET_ID):
        group, needs = 'audio', ['开始画面', '一段表演音频', '一句动作描述']
        summary = ('对白时间轴会锁进视频，人物按这段声音开口。'
                   if preset_id == LOCKED_PRESET_ID else
                   '用一段声音引导人物怎么动，不限制结束姿态，适合转身、行走。')
    else:
        group, needs = 'first', ['开始画面', '一句动作描述']
        summary = '上传或从素材库拼一张开始画面，写清动作与镜头即可。'

    # 同一组里的档位差别必须说出来，否则五张卡写同一句话等于没帮用户选。
    summary = _PLAIN_SUMMARY.get(preset_id, summary)

    fast = preset_id in (FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID)
    quality = preset_id in QUALITY_PRESET_IDS
    if fast:
        badge = '本机实测 · 相对最快'
    elif quality:
        badge = '仅对照 · 慢 1.8 倍'
    elif preset_id in ADULT_PRESET_IDS:
        badge = '真人质感 LoRA'
    elif group == 'frames':
        badge = '需要两张图'
    elif group == 'audio':
        badge = '需要额外音频'
    else:
        badge = ''
    caveat = ''
    if preset_id == LOCKED_PRESET_ID:
        caveat = '需要已定稿的对白时间轴；口型与无字幕画面尚未做本机小样。'
    elif quality:
        caveat = '实测画质无明显提升（211s → 381s），只是换了动作轨迹。不是「更清晰」的档，别拿它出成片。'
    elif preset_id in (NATIVE_PORTRAIT_PRESET_ID, TEXT_PORTRAIT_PRESET_ID, *END_PRESET_IDS):
        caveat = '这一档还没做本机画质小样，建议先用短片段试一次。'
    elif preset_id in ADULT_PRESET_IDS:
        caveat = '仅限虚构成年人内容；提示词前的 r34l1sm 会自动补，不需要自己写。'
    return {
        'group': group, 'needs': needs, 'summary': summary,
        'speed': 'fast' if fast else 'quality' if quality else 'standard',
        'sound': 'native' if group in ('text', 'first', 'frames') else 'guide',
        'badge': badge, 'caveat': caveat,
    }


def trigger_prefix(preset_id: str | None) -> str:
    """现实人像 LoRA 的触发词。

    fal 的 H3 Realism People 只在提示词里出现 `r34l1sm` 时才真正起作用；
    LoRA 文件放对位置但没有这个词，效果和底模几乎一样。统一在这里给出前缀，
    三个入口（分镜、单步工具、批量重跑）就不必各写一遍。
    """
    return f'{ADULT_TRIGGER}, ' if preset_id in ADULT_PRESET_IDS else ''


def stage_id(preset_id: str = PRESET_ID) -> str:
    title(preset_id)
    if preset_id in PIKA_PRESET_IDS:
        return preset_id.replace('h3-', 'motion-') + '-generation'
    if preset_id in (*TEXT_PRESET_IDS, *END_PRESET_IDS, NATIVE_PORTRAIT_PRESET_ID, *QUALITY_PRESET_IDS):
        return preset_id.replace('h3-', 'motion-') + '-generation'
    if preset_id == LOCKED_PRESET_ID:
        return 'motion-first-locked-audio-generation'
    if preset_id == NATIVE_LANDSCAPE_PRESET_ID:
        return 'motion-first-native-landscape-generation'
    if preset_id == NATIVE_SQUARE_PRESET_ID:
        return 'motion-first-native-square-generation'
    if preset_id == FAST_IMAGE_LANDSCAPE_PRESET_ID:
        return 'motion-first-fast-kitchen-generation'
    if preset_id == ADULT_REALISM_IMAGE_PRESET_ID:
        return 'motion-first-realism-people-generation'
    return 'motion-first-generation' if preset_id == FIRST_PRESET_ID else 'motion-generation'


preset_stage_id = stage_id


def load_recipe(preset_id: str = PRESET_ID) -> dict[str, Any]:
    if preset_id in PIKA_PRESET_IDS:
        graph = json.loads((CONTRACTS / 'pika-t2va-api.json').read_text(encoding='utf-8'))
        # Pika 官方 6 步基线：生成 124 帧，交付裁到 120 帧 / 5.0 秒；
        # 0.6MP 在 16:9 的网格尺寸是 1056×608。
        graph['8']['inputs'].update(width=1056, height=608, length=124)
        graph['14']['inputs'].update(duration_seconds=5.0, frame_count=120)
        return graph
    title(preset_id)
    if preset_id == FAST_TEXT_LANDSCAPE_PRESET_ID:
        graph = load_recipe(TEXT_LANDSCAPE_PRESET_ID)
    elif preset_id == FAST_IMAGE_LANDSCAPE_PRESET_ID:
        graph = load_recipe(NATIVE_LANDSCAPE_PRESET_ID)
    elif preset_id == ADULT_REALISM_TEXT_PRESET_ID:
        graph = load_recipe(TEXT_LANDSCAPE_PRESET_ID)
    elif preset_id == ADULT_REALISM_IMAGE_PRESET_ID:
        graph = load_recipe(NATIVE_LANDSCAPE_PRESET_ID)
    elif preset_id in QUALITY_PRESET_IDS:
        graph = load_recipe(NATIVE_LANDSCAPE_PRESET_ID)
    else:
        graph = json.loads((CONTRACTS / 'h3-guided-api.json').read_text(encoding='utf-8'))
    # 档位参数对所有方案生效（含首尾帧 / 音频驱动档）。以前只写在
    # `if preset_id in NATIVE_PRESET_IDS` 里，导致非 native 方案一直沿用契约图里的旧值。
    graph['115']['inputs'].update(
        aspect_ratio={'1:1': '1:1 (Square)', '16:9': '16:9 (Widescreen)',
                      '9:16': '9:16 (Portrait Widescreen)'}[aspect_ratio(preset_id)],
        megapixels=megapixels(preset_id))
    graph['105:124']['inputs']['value'] = hardware.steps('quality')
    graph['105:125']['inputs']['value'] = hardware.steps('fast')
    graph['105:126']['inputs']['value'] = turbo_default(preset_id)
    if preset_id in (FIRST_PRESET_ID, LOCKED_PRESET_ID, *NATIVE_PRESET_IDS) and preset_id not in END_PRESET_IDS:
        graph.pop('ip-video:last-frame:2', None)
        for node in graph.values():
            node['inputs'].pop('last_frame', None)
    if preset_id in NATIVE_PRESET_IDS:
        graph.pop('aigc:add-audio-guide', None)
        graph.pop('aigc:load-audio-guide', None)
        graph['105:16']['inputs']['conditioning'] = ['105:104', 0]
    if preset_id in TEXT_PRESET_IDS:
        graph.pop('114', None)
        graph['105:104']['inputs'].pop('first_frame', None)
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
    if preset_id in (FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID):
        # These are the recipes tested in the local H3 acceleration run. Keep
        # the same input graph and only replace the model path/sampling branch.
        model = ['105:6', 0]
        graph['105:121']['inputs'].update(model=model)
        if preset_id == FAST_TEXT_LANDSCAPE_PRESET_ID:
            graph['105:121']['inputs']['lora_name'] = 'minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors'
            graph['105:17']['inputs']['sampler_name'] = 'euler'
            graph['105:9']['inputs']['scheduler'] = 'beta'
        graph['105:126']['inputs']['value'] = True
        graph['105:9']['inputs']['steps'] = hardware.steps('fast')
        shifted = 'h3-fast-sigma-shift'
        graph[shifted] = {'class_type': 'MiniMaxH3SigmaShift',
                          'inputs': {'model': ['105:121', 0],
                                     'shift_video': hardware.profile().sigma_shift_fast,
                                     'shift_audio': 5.0}}
        graph['h3-fast-kitchen'] = {'class_type': 'ModelAttentionBackend',
                                    'inputs': {'model': [shifted, 0], 'attention': 'comfy kitchen attention'}}
        graph['105:9']['inputs']['model'] = ['h3-fast-kitchen', 0]
        graph['105:16']['inputs']['model'] = ['h3-fast-kitchen', 0]
    if preset_id in ADULT_PRESET_IDS:
        # Turbo 开关（105:126）与真人 LoRA 是两件事：开关只该决定「走不走 8 步加速」。
        # 原来把 LoRA 名写进 105:121，但 105:122 的 on_false 直连底模，而
        # ComfySwitchNode 是 lazy 的——未选中的分支整条不求值，于是 105:121 被
        # 完全绕过。实测同 seed 下「原样」与「把 on_false 接到 LoRA」两版输出
        # 平均差 8.9、42% 像素差 >10，证明开 LoRA 前后是两个模型。
        # 现在把 LoRA 挂在开关之后，无论开关选哪一路都会生效。
        graph['aigc:adult-lora'] = {
            'class_type': 'LoraLoaderModelOnly',
            'inputs': {'model': ['105:122', 0],
                       'lora_name': r'adult-safe\h3-realism-people-t2v-i2v-r2v.safetensors',
                       'strength_model': 1.0},
        }
        graph['105:9']['inputs']['model'] = ['aigc:adult-lora', 0]
        graph['105:16']['inputs']['model'] = ['aigc:adult-lora', 0]
        graph['105:126']['inputs']['value'] = False
    return graph


def input_specs(preset_id: str = PRESET_ID) -> list[dict[str, Any]]:
    title(preset_id)
    if preset_id in PIKA_PRESET_IDS:
        document = json.loads((CONTRACTS / 'pipeline-inputs.json').read_text(encoding='utf-8'))
        specs = [dict(item) for item in document['stages']['motion-generation']['inputs']
                 if item['id'] in ('prompt', 'seed', 'duration')]
        for spec in specs:
            if spec['id'] == 'prompt':
                spec['binding'] = {'node_id': '8', 'input': 'prompt'}
                spec['description'] = '描述画面、动作、镜头和声音；Pika 会把声音一起生成。'
            elif spec['id'] == 'seed':
                spec['binding'] = {'node_id': '11', 'input': 'noise_seed'}
            elif spec['id'] == 'duration':
                spec.update(min=5.0, max=5.0, step=0.0001,
                            description='Pika 官方 6 步基线：生成 124 帧，交付裁到 120 帧 / 5.0 秒。')
                spec['binding'] = {'node_id': '14', 'input': 'duration_seconds'}
        return specs
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


def _technical_description(preset_id: str) -> str:
    """折叠在「技术细节」里的说明。所有数值从档位读，不写死。"""
    hw = hardware.profile()
    if preset_id in PIKA_PRESET_IDS:
        return (f'Pika H3 Flex · 6 步 LCM · 联合音视频 · {aspect_ratio(preset_id)} · '
                f'768p · 交付 5.0 秒（124 帧生成 / 120 帧输出） · 硬件档位：{hw.gpu_name} / {hw.vram_gib} GiB / {hw.key}。')
    if preset_id in END_PRESET_IDS:
        detail = '首尾帧 + H3 原生声音（待小样）'
    elif preset_id == FAST_TEXT_LANDSCAPE_PRESET_ID:
        detail = 'v4 Turbo LoRA + Kitchen attention'
    elif preset_id == FAST_IMAGE_LANDSCAPE_PRESET_ID:
        detail = 'Turbo + Kitchen attention + sigma shift'
    elif preset_id in ADULT_PRESET_IDS:
        detail = 'Realism People LoRA · 关闭 Turbo'
    elif preset_id in QUALITY_PRESET_IDS:
        detail = '关闭 Turbo · 完整采样'
    elif preset_id == LOCKED_PRESET_ID:
        detail = '锁定对白 latent（T8）'
    elif preset_id in TEXT_PRESET_IDS:
        detail = '纯文本 + Turbo 采样'
    else:
        detail = '首帧 + H3 原生声音'
    if preset_id in ADULT_PRESET_IDS or preset_id in QUALITY_PRESET_IDS:
        kind = 'quality'
    elif preset_id in (FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID):
        kind = 'fast'
    else:
        kind = 'standard'
    return (f'H3 · {detail} · {aspect_ratio(preset_id)} · {megapixels(preset_id)}MP · '
            f'{hardware.steps(kind)} 步 · 4–15 秒。'
            f'硬件档位：{hw.gpu_name} / {hw.vram_gib} GiB / {hw.key}。')


def catalog(model_root: Path, preset_id: str = PRESET_ID) -> dict[str, Any]:
    label = title(preset_id)
    labels = {'turbo': turbo_default(preset_id)}
    try:
        graph = load_recipe(preset_id)
        inputs = input_specs(preset_id)
        for spec in inputs:
            binding = spec['binding']
            graph[binding['node_id']]['inputs'][binding['input']]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'id': preset_id, 'title': label, 'available': False, 'reason': f'制作方案文件不完整：{exc}',
                'missing_models': [], **labels, **guide(preset_id)}
    categories = {'unet_name': ('diffusion_models', 'unet'), 'clip_name': ('text_encoders', 'clip'), 'vae_name': ('vae',), 'lora_name': ('loras',)}
    missing = []
    for node in graph.values():
        for key, directories in categories.items():
            name = node['inputs'].get(key)
            if name and not any(any((model_root / directory).rglob(name)) for directory in directories):
                missing.append(name)
    return {
        'id': preset_id, 'title': label, 'available': True, 'aspect_ratio': aspect_ratio(preset_id),
        'description': _technical_description(preset_id),
        # 这套方案默认走不走 8 步 Turbo：界面据此初始化开关，而不是一律写死 enable。
        # 写死的后果是成人向/精细方案实际跑的是「Turbo LoRA + 真人 LoRA」双串，
        # 实测 534 秒，比它自己写的 20 步（361 秒）还慢。
        'turbo': turbo_default(preset_id),
        'steps': _steps_for(preset_id, graph),
        # 方案的原生采样档（百万像素）：480p≈0.4、768p≈0.6。前端据此初始化原生档按钮，
        # 避免统一写死 0.6 把「快速方案」的 0.4 顶掉。
        'megapixels': megapixels(preset_id),
        'verification': (
            '本机 5070 Ti 16GB / cu130 已跑通快速档；其余档位按 hardware 档位自动匹配，生成后仍需逐条审核。'
            if preset_id in (FAST_TEXT_LANDSCAPE_PRESET_ID, FAST_IMAGE_LANDSCAPE_PRESET_ID) else
            '档位参数由 backend/hardware.py 按显存自动给出；换卡后会跟着变，无需手改这里。'),
        'missing_models': sorted(set(missing)),
        'model_requirements': model_requirements(preset_id, model_root),
        **guide(preset_id),
    }


MODEL_EXTENSIONS = ('.safetensors', '.gguf', '.pth', '.pt', '.bin', '.onnx')
MODEL_ROLES = {
    'diffusion_models': 'diffusion_model', 'unet': 'diffusion_model', 'loras': 'lora',
    'text_encoders': 'text_encoder', 'clip': 'text_encoder', 'vae': 'vae',
    'audio_encoders': 'audio_encoder', 'latent_upscale_models': 'latent_upscaler',
    'upscale_models': 'upscaler', 'vae_approx': 'vae_approx', 'LLM': 'llm', 'AuK': 'voice_model',
}


def _model_metadata(relative: str, size_bytes: int) -> dict[str, Any]:
    name = relative.rsplit('/', 1)[-1]
    lower = name.lower()
    precision = next((token for token in ('nvfp4', 'int8', 'fp8', 'bf16', 'fp16', 'fp32', 'q4_k_m', 'q8_0', 'gguf') if token in lower), 'unknown')
    family = next((token for token in ('minimax_h3', 'qwen3vl', 'qwen_image', 'qwen_2.5', 'wan2.2', 'krea2', 'anima', 'auk', 'fastvideo', 'noctq') if token in lower), 'other')
    category = relative.split('/', 1)[0]
    return {
        'id': relative, 'filename': name, 'category': category,
        'role': MODEL_ROLES.get(category, 'other'), 'format': name.rsplit('.', 1)[-1].lower(),
        'precision': precision, 'family': family, 'size_bytes': size_bytes,
    }


def _model_inventory(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 0:
                files.append(_model_metadata(path.relative_to(root).as_posix(), size))
    return files


def _requirement_paths(preset_id: str) -> list[tuple[str, str]]:
    """Return (relative name, expected category) pairs referenced by a preset graph."""
    graph = load_recipe(preset_id)
    found: list[tuple[str, str]] = []
    for key, category in (('unet_name', 'diffusion_models'), ('clip_name', 'text_encoders'), ('vae_name', 'vae'), ('lora_name', 'loras')):
        for node in graph.values():
            value = node.get('inputs', {}).get(key)
            if isinstance(value, str) and value:
                normalized = value.replace('\\', '/')
                found.append((normalized, category))
    return sorted(set(found))


def model_requirements(preset_id: str, model_root: Path) -> list[dict[str, Any]]:
    """Describe the model files a preset eats; no recommendation, only facts."""
    title(preset_id)
    inventory = {item['filename']: item for item in _model_inventory(model_root)}
    result = []
    for name, category in _requirement_paths(preset_id):
        record = inventory.get(name.rsplit('/', 1)[-1])
        result.append({
            'id': record['id'] if record else f'{category}/{name}',
            'filename': name.rsplit('/', 1)[-1], 'category': category,
            'role': MODEL_ROLES.get(category, 'other'),
            'present': bool(record), 'size_bytes': record['size_bytes'] if record else 0,
        })
    return result


def model_catalog(model_root: Path, category: str | None = None, query: str | None = None) -> dict[str, Any]:
    """Read-only inventory of local model files, for Agent composition."""
    files = _model_inventory(model_root)
    usage: dict[str, list[str]] = {}
    for preset_id in PRESET_IDS:
        try:
            requirements = model_requirements(preset_id, model_root)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for requirement in requirements:
            usage.setdefault(requirement['filename'], []).append(preset_id)
    for item in files:
        item['used_by'] = sorted(usage.get(item['filename'], []))
    if category:
        files = [item for item in files if item['category'] == category]
    if query:
        needle = query.lower()
        files = [item for item in files if needle in item['filename'].lower() or needle in item['family'].lower()]
    categories: dict[str, dict[str, Any]] = {}
    for item in _model_inventory(model_root):
        value = categories.setdefault(item['category'], {'files': 0, 'size_bytes': 0, 'roles': []})
        value['files'] += 1
        value['size_bytes'] += item['size_bytes']
        if item['role'] not in value['roles']:
            value['roles'].append(item['role'])
    return {
        'schema_version': 1, 'root': 'ComfyUI-Shared/models', 'download_enabled': False,
        'generated_from': 'read-only local inventory', 'categories': categories,
        'files': files,
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
