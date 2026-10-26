"""命名设定库：把你显式填写过的配乐／配音参数存成可复用的设定。

设定只存在本机数据根（director-data/config），不进仓库；载入与保存都由用户点击，
不会替任何人挑选参数。
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

from . import dubbing, emotion, music

KINDS = ('music', 'dubbing')
MAX_SETTINGS = 40
MAX_NAME = 40
_LOCK = threading.Lock()

# 配乐设定里可以复用的参数：随机编号不存（每次新生成应当重新决定）。
MUSIC_FIELDS = ('tags', 'lyrics', 'duration_seconds', 'bpm', 'keyscale', 'timesignature', 'language',
                'steps', 'cfg', 'shift', 'output_format', 'quality', 'generate_audio_codes',
                'encode_cfg_scale', 'temperature', 'top_p', 'top_k', 'min_p')
# 配音设定存随机编号：VoxCPM2 的音色由描述+随机编号共同决定，换随机编号就不是同一个声音。
DUBBING_FIELDS = ('engine', 'voice_reference', 'voice_description', 'seed',
                 'original_audio', 'original_gain', 'emotion')


def document_path() -> Path:
    # Read the workbench root at call time so tests and private layouts can redirect it.
    from . import app as backend
    return Path(backend.ROOT) / 'config' / 'creative-settings.json'


def clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError('设定名称必须是文字')
    label = ''.join(char for char in name if unicodedata.category(char)[0] != 'C' and char not in '<>:"/\\|?*').strip(' .')
    if not label or len(label) > MAX_NAME:
        raise ValueError(f'设定名称须为 1 至 {MAX_NAME} 个字符')
    if not re.search(r'[^\s]', label):
        raise ValueError('设定名称不能只有空白字符')
    return label


def load(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    document = {'music': [], 'dubbing': []}
    target = path or document_path()
    try:
        stored = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return document
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法读取命名设定: {exc}') from exc
    if not isinstance(stored, dict):
        raise ValueError('命名设定文件不是 JSON 对象')
    for kind in KINDS:
        entries = stored.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f'命名设定的 {kind} 区不是列表')
        document[kind] = [dict(item) for item in entries if isinstance(item, dict) and item.get('name')]
    return document


def validate_params(kind: str, params: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError('未知设定分区')
    if not isinstance(params, dict):
        raise ValueError('设定参数必须是 JSON 对象')
    if kind == 'music':
        fields = {key: params[key] for key in MUSIC_FIELDS if key in params}
        missing = [key for key in MUSIC_FIELDS if key not in params]
        if missing:
            raise ValueError(f'配乐设定缺少参数：{"、".join(missing)}')
        if set(params) - set(MUSIC_FIELDS) - {'seed', 'idempotency_key'}:
            raise ValueError('配乐设定包含未知参数')
        music.validate({**fields, 'seed': params.get('seed') if type(params.get('seed')) is int else 0})
        return fields
    extra = set(params) - set(DUBBING_FIELDS) - {'cues', 'source_asset_id', 'idempotency_key'}
    if extra:
        raise ValueError('配音设定包含未知参数')
    engine = params.get('engine') or dubbing.DEFAULT_ENGINE
    if engine not in dubbing.ENGINES:
        raise ValueError('配音设定里的引擎未知，请重新选择')
    reference = str(params.get('voice_reference') or '').strip()
    description = str(params.get('voice_description') or '').strip()
    if reference and description:
        raise ValueError('配音设定只能选一种音色来源：参考音频或文字描述')
    if not reference and not description:
        raise ValueError('配音设定必须包含音色参考或音色描述')
    if len(reference) > 1000 or len(description) > 2000:
        raise ValueError('配音设定的音色来源过长')
    if engine == 'indextts-2.5' and description:
        raise ValueError('IndexTTS 2.5 只沿用参考音色，请改用 VoxCPM2 保存音色描述')
    if params.get('original_audio') not in {'keep', 'mute'}:
        raise ValueError('配音设定的原片音轨只能是保留或静音')
    gain = params.get('original_gain')
    if type(gain) not in (int, float) or not 0 <= gain <= 3:
        raise ValueError('配音设定的原片音量须在 0 至 3 之间')
    frozen: dict[str, Any] = {'engine': engine, 'original_audio': params['original_audio'],
                              'original_gain': float(gain)}
    if description:
        frozen['voice_description'] = description
    else:
        frozen['voice_reference'] = reference
    if engine == 'voxcpm-2':
        seed = params.get('seed')
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError('VoxCPM2 配音设定须包含 32 位非负整数随机编号')
        frozen['seed'] = seed
    if params.get('emotion') not in (None, {'mode': 'none'}):
        # A voice setting may carry an emotion, but the mode has to exist on that engine.
        frozen_emotion = emotion.validate(params['emotion'], text_engine_ready=True)
        allowed = dubbing.EMOTION_MODES[engine] or {'none': True, 'reference': True, 'text': True, 'vector': True}
        if not allowed.get(frozen_emotion['mode']):
            raise ValueError(f"{dubbing.ENGINES[engine]['label']} 配音设定不支持这种情绪方式")
        frozen['emotion'] = frozen_emotion
    return frozen


def save(kind: str, name: str, params: dict[str, Any], *, now: float, path: Path | None = None) -> dict[str, Any]:
    label, frozen = clean_name(name), validate_params(kind, params)
    with _LOCK:
        document = load(path)
        entry = {'name': label, 'params': frozen, 'saved_at': now}
        others = [item for item in document[kind] if item.get('name') != label]
        if len(others) >= MAX_SETTINGS:
            raise ValueError(f'每个分区最多保存 {MAX_SETTINGS} 条设定')
        document[kind] = others + [entry]
        _write(document, path)
    return entry


def remove(kind: str, name: str, path: Path | None = None) -> bool:
    label = clean_name(name)
    with _LOCK:
        document = load(path)
        others = [item for item in document[kind] if item.get('name') != label]
        if len(others) == len(document[kind]):
            return False
        document[kind] = others
        _write(document, path)
    return True


def _write(document: dict[str, list[dict[str, Any]]], path: Path | None) -> None:
    from . import app as backend
    target = path or document_path()
    backend.write_json_atomic(target, {kind: sorted(document[kind], key=lambda item: str(item.get('name')))
                                       for kind in KINDS})
