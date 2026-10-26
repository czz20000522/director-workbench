"""IndexTTS-2.5 情绪控制：只暴露这台机器上真实跑得通的模式。

情绪与音色是分开的两件事：音色永远来自参考音频，这里控制的是“同一把声音怎么说”。
向量模式沿用官方 webui 的预处理（先按偏差系数缩小，再保证总和不超过 0.8），
否则引擎会收到官方界面永远不会发的数值。
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

EMOTIONS = ('happy', 'angry', 'sad', 'afraid', 'disgusted', 'melancholic', 'surprised', 'calm')
MODES = ('none', 'reference', 'text', 'vector')
# indextts/infer_v2_5.py normalize_emo_vec() 的官方偏差与总量上限。
EMO_BIAS = (0.9375, 0.875, 1.0, 1.0, 0.9375, 0.9375, 0.6875, 0.5625)
EMO_MAX_SUM = 0.8
MAX_TEXT = 200
REFERENCE_SUFFIXES = {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg'}


def modes(*, text_engine_ready: bool) -> dict[str, bool]:
    return {'none': True, 'reference': True, 'vector': True, 'text': text_engine_ready}


def normalize_vector(values: Any) -> list[float]:
    if not isinstance(values, list) or len(values) != len(EMOTIONS):
        raise ValueError(f'情绪向量须为 {len(EMOTIONS)} 个数值，顺序：{"、".join(EMOTIONS)}')
    scaled = []
    for value, bias in zip(values, EMO_BIAS):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('每个情绪强度须为 0 至 1 的有限数值')
        scaled.append(float(value) * bias)
    total = sum(scaled)
    if total > EMO_MAX_SUM:
        scaled = [value * EMO_MAX_SUM / total for value in scaled]
    return [round(value, 6) for value in scaled]


def validate(emotion: Any, *, text_engine_ready: bool) -> dict[str, Any]:
    """Turn the submitted emotion into the exact arguments the engine will receive."""
    if emotion in (None, {}):
        return {'mode': 'none'}
    if not isinstance(emotion, dict):
        raise ValueError('情绪设置必须是 JSON 对象')
    # Unset optional knobs arrive as null from the form; treat them as absent.
    emotion = {key: value for key, value in emotion.items() if value is not None}
    allowed = {'mode', 'alpha', 'reference', 'text', 'vector'}
    if set(emotion) - allowed:
        raise ValueError('情绪设置包含未知字段')
    mode = emotion.get('mode', 'none')
    if mode not in MODES:
        raise ValueError('未知情绪模式')
    if mode == 'none':
        if set(emotion) - {'mode'}:
            raise ValueError('不控制情绪时不能附带其他情绪字段')
        return {'mode': 'none'}
    if mode == 'text' and not text_engine_ready:
        raise ValueError('本机缺少文本情绪模型（qwen0.6bemo4-merge），请改用参考音频或向量')
    alpha = emotion.get('alpha', 1.0)
    if type(alpha) not in (int, float) or not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise ValueError('情绪强度须在 0 至 1 之间')
    result: dict[str, Any] = {'mode': mode, 'use_qwen_emo': mode == 'text'}
    if mode == 'reference':
        if not isinstance(emotion.get('reference'), str) or not emotion['reference'].strip():
            raise ValueError('情绪参考模式必须选择一段参考音频')
        result['reference'] = emotion['reference'].strip()
        result['alpha'] = float(alpha)
        if emotion.get('text') or emotion.get('vector'):
            raise ValueError('情绪参考模式不能同时提供文本或向量')
    elif mode == 'vector':
        result['vector'] = normalize_vector(emotion.get('vector'))
        if emotion.get('reference') or emotion.get('text'):
            raise ValueError('情绪向量模式不能同时提供参考音频或文本')
    else:
        text = str(emotion.get('text') or '').strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f'情绪描述须为 1 至 {MAX_TEXT} 字符')
        result['text'] = text
        if emotion.get('reference') or emotion.get('vector'):
            raise ValueError('情绪文本模式不能同时提供参考音频或向量')
    return result


def freeze(emotion: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Copy the emotion reference into the task directory so the request stays frozen."""
    if emotion.get('mode') != 'reference':
        return {**emotion, 'reference': None}
    source = Path(emotion['reference']).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in REFERENCE_SUFFIXES:
        raise ValueError('情绪参考必须是可读取的音频文件')
    frozen = directory / ('emo-reference' + source.suffix.lower())
    shutil.copyfile(source, frozen)
    return {**emotion, 'reference': str(frozen)}


def infer_arguments(emotion: dict[str, Any]) -> dict[str, Any]:
    """Adapter-side mapping: the frozen request already decided everything."""
    mode = emotion.get('mode', 'none')
    if mode == 'none':
        return {}
    if mode == 'reference':
        return {'emo_audio_prompt': emotion['reference'], 'emo_alpha': float(emotion.get('alpha', 1.0))}
    if mode == 'vector':
        return {'emo_vector': emotion['vector']}
    return {'use_emo_text': True, 'emo_text': emotion['text']}
