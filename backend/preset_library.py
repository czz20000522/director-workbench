"""音色库与配乐库：起点预设卡片，不是你的参数集合。

内置卡片写在这个文件里（可带入，不能删）；你自己存的卡片落在
``<数据根>/config/preset-library.json``，同样不进仓库。两者一起返回，同名时
你的那张覆盖内置那张。「带入」永远是一次显式点击，不会替任何面板填参数。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import music
from .creative_settings import clean_name

KINDS = ('timbre', 'music')
MAX_CARDS = 40
TIMBRE_ENGINES = ('voxcpm-2', 'indextts-2.5')
_LOCK = threading.Lock()

# 全部为虚构成年人音色：不写真人、公众人物或未成年人的样子。
BUILTIN: dict[str, list[dict[str, Any]]] = {
    'timbre': [
        {'name': '沉稳中年旁白', 'params': {'engine': 'voxcpm-2',
         'voice_description': '四十岁上下的男性，音色低沉干净，语速平稳，气息很稳，像纪录片旁白'}},
        {'name': '沧桑老者', 'params': {'engine': 'voxcpm-2',
         'voice_description': '七十岁左右的男性，声音沙哑带颗粒感，语速很慢，句子结尾往下沉'}},
        {'name': '清亮青年女声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '二十七八岁的女性，音色清亮偏薄，语速中等，尾音略微上扬'}},
        {'name': '低沉克制女声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '三十五岁左右的女性，音色偏低，音量不大，咬字干净，情绪收着说'}},
        {'name': '干练解说女声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '三十二岁左右的女性，音色结实干脆，语速偏快，咬字靠前，适合解说'}},
        {'name': '疲惫中年男声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '四十五岁左右的男性，声音发闷，语速偏慢，像刚加完班的状态'}},
        {'name': '宽厚长辈男声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '六十岁上下的男性，音色宽厚，语速舒缓，句子之间留白多'}},
        {'name': '冷淡年轻男声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '二十八岁左右的男性，音色偏薄偏冷，语速慢，情绪很淡，尾音短'}},
        {'name': '争执中的男声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '三十八岁左右的男性，音量偏大，语速快，句子之间抢气，像在跟人争执'}},
        {'name': '忍着哭的女声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '三十岁左右的女性，音色柔和，句首带气声，像忍着情绪把话说完'}},
        {'name': '商业播报女声', 'params': {'engine': 'voxcpm-2',
         'voice_description': '三十五岁左右的女性，音色明亮有弹性，字头清晰，语速均匀，像商业播报'}},
    ],
    'music': [
        {'name': '悬疑铺底', 'params': {'tags': '悬疑氛围，弦乐铺底与低频脉冲，克制，无人声', 'bpm': 88,
         'keyscale': 'A minor', 'timesignature': '4', 'duration_seconds': 24, 'language': 'unknown'}},
        {'name': '追逐紧张', 'params': {'tags': '紧张追逐，打击乐驱动，铜管刺点，节奏快', 'bpm': 140,
         'keyscale': 'D minor', 'timesignature': '4', 'duration_seconds': 30, 'language': 'unknown'}},
        {'name': '抒情钢琴', 'params': {'tags': '抒情钢琴与弦乐，温暖缓慢，留白多', 'bpm': 72,
         'keyscale': 'C major', 'timesignature': '4', 'duration_seconds': 30, 'language': 'unknown'}},
        {'name': '夜色独白', 'params': {'tags': '夜晚独白氛围，电钢琴与低频铺底，极简', 'bpm': 66,
         'keyscale': 'F minor', 'timesignature': '4', 'duration_seconds': 24, 'language': 'unknown'}},
        {'name': '古风史诗', 'params': {'tags': '古风史诗，鼓阵与箫，弦乐渐强，宏大', 'bpm': 96,
         'keyscale': 'E minor', 'timesignature': '4', 'duration_seconds': 30, 'language': 'unknown'}},
        {'name': '日常轻快', 'params': {'tags': '日常轻快，木吉他与口琴，明亮，没有压迫感', 'bpm': 112,
         'keyscale': 'G major', 'timesignature': '4', 'duration_seconds': 20, 'language': 'unknown'}},
        {'name': '悲伤弦乐', 'params': {'tags': '悲伤弦乐，慢速大提琴，克制，逐渐消散', 'bpm': 60,
         'keyscale': 'A minor', 'timesignature': '3', 'duration_seconds': 30, 'language': 'unknown'}},
        {'name': '电子悬疑', 'params': {'tags': '电子悬疑，合成器琶音与侧链脉冲，冷硬', 'bpm': 128,
         'keyscale': 'B minor', 'timesignature': '4', 'duration_seconds': 24, 'language': 'unknown'}},
    ],
}


def document_path() -> Path:
    # Read the workbench root at call time so tests and private layouts can redirect it.
    from . import app as backend
    return Path(backend.ROOT) / 'config' / 'preset-library.json'


def load(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    document = {kind: [] for kind in KINDS}
    target = path or document_path()
    try:
        stored = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return document
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法读取预设库: {exc}') from exc
    if not isinstance(stored, dict):
        raise ValueError('预设库文件不是 JSON 对象')
    for kind in KINDS:
        entries = stored.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f'预设库的 {kind} 区不是列表')
        document[kind] = [dict(item) for item in entries if isinstance(item, dict) and item.get('name')]
    return document


def validate_params(kind: str, params: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError('未知预设分区')
    if not isinstance(params, dict):
        raise ValueError('预设参数必须是 JSON 对象')
    if kind == 'music':
        return _validate_music(params)
    return _validate_timbre(params)


def _text(value: Any, name: str, limit: int, required: bool) -> str:
    text = str(value or '').strip()
    if not text:
        if required:
            raise ValueError(f'预设卡片缺少{name}')
        return ''
    if len(text) > limit:
        raise ValueError(f'{name}最长 {limit} 个字符')
    return text


def _validate_timbre(params: dict[str, Any]) -> dict[str, Any]:
    if set(params) - {'engine', 'voice_description', 'voice_reference', 'text', 'generated_by'}:
        raise ValueError('音色卡片包含未知参数')
    engine = params.get('engine') or 'voxcpm-2'
    if engine not in TIMBRE_ENGINES:
        raise ValueError('音色卡片里的引擎未知，请重新选择')
    generated_by = params.get('generated_by')
    if generated_by is not None and generated_by not in TIMBRE_ENGINES:
        raise ValueError('音色卡片的生成引擎未知')
    description = _text(params.get('voice_description'), '音色描述', 2000, engine == 'voxcpm-2')
    reference = _text(params.get('voice_reference'), '参考音频', 1000, engine == 'indextts-2.5')
    if engine == 'voxcpm-2' and reference:
        raise ValueError('VoxCPM2 音色卡片只写描述，不要带参考音频')
    if engine == 'indextts-2.5' and description:
        raise ValueError('IndexTTS 2.5 音色卡片只能指向一段参考音频，它不能凭文字造音色')
    frozen: dict[str, Any] = {'engine': engine}
    if description:
        frozen['voice_description'] = description
    else:
        frozen['voice_reference'] = reference
    text = _text(params.get('text'), '试听台词', 2000, False)
    if text:
        frozen['text'] = text
    # 这张卡最初由哪个引擎做出来（用在归档样本上：它可能只是拿来当参考，
    # 生成它的引擎和「带入时用哪个引擎」是两件事）。
    if generated_by:
        frozen['generated_by'] = generated_by
    return frozen


def _validate_music(params: dict[str, Any]) -> dict[str, Any]:
    if set(params) - {'tags', 'bpm', 'keyscale', 'timesignature', 'duration_seconds', 'language'}:
        raise ValueError('配乐卡片包含未知参数')
    tags = _text(params.get('tags'), '风格标签', 2000, True)
    bpm = params.get('bpm')
    if type(bpm) is not int or not 10 <= bpm <= 300:
        raise ValueError('配乐卡片的 BPM 须为 10 至 300 的整数')
    keyscale = _text(params.get('keyscale'), '调性', 16, True)
    timesignature = str(params.get('timesignature') or '4')
    if timesignature not in music.TIME_SIGNATURES:
        raise ValueError(f"配乐卡片的拍号只能是 {'、'.join(music.TIME_SIGNATURES)}")
    duration = params.get('duration_seconds')
    if type(duration) not in (int, float) or not 1 <= duration <= 300:
        raise ValueError('配乐卡片的时长须在 1 至 300 秒之间')
    language = str(params.get('language') or 'unknown')
    if language not in music.LANGUAGES:
        raise ValueError('配乐卡片的歌词语言不在引擎支持范围内')
    return {'tags': tags, 'bpm': bpm, 'keyscale': keyscale, 'timesignature': timesignature,
            'duration_seconds': float(duration), 'language': language}


def cards(kind: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Built-ins first, then the user's own cards; a saved name replaces its built-in."""
    if kind not in KINDS:
        raise ValueError('未知预设分区')
    mine = {str(item.get('name')): item for item in load(path)[kind]}
    merged = []
    for builtin in BUILTIN[kind]:
        name = str(builtin['name'])
        saved = mine.pop(name, None)
        merged.append({'name': name, 'params': saved['params'] if saved else builtin['params'],
                       'builtin': True, 'modified': bool(saved)})
    merged.extend({'name': name, 'params': item['params'], 'builtin': False, 'modified': False}
                  for name, item in sorted(mine.items()))
    return merged


def save(kind: str, name: str, params: dict[str, Any], *, now: float | None = None,
         path: Path | None = None) -> dict[str, Any]:
    label, frozen = clean_name(name), validate_params(kind, params)
    with _LOCK:
        document = load(path)
        others = [item for item in document[kind] if item.get('name') != label]
        if len(others) >= MAX_CARDS:
            raise ValueError(f'每个分区最多保存 {MAX_CARDS} 张卡片')
        entry = {'name': label, 'params': frozen, 'saved_at': time.time() if now is None else now}
        document[kind] = others + [entry]
        _write(document, path)
    return entry


def remove(kind: str, name: str, path: Path | None = None) -> bool:
    """Delete a saved card. Removing a same-name override puts the built-in card back."""
    if kind not in KINDS:
        raise ValueError('未知预设分区')
    label = clean_name(name)
    with _LOCK:
        document = load(path)
        others = [item for item in document[kind] if item.get('name') != label]
        if len(others) == len(document[kind]):
            if any(str(item['name']) == label for item in BUILTIN[kind]):
                raise ValueError('内置卡片不能删除；想改就同名另存一张你自己的')
            return False
        document[kind] = others
        _write(document, path)
    return True


def _write(document: dict[str, list[dict[str, Any]]], path: Path | None) -> None:
    from . import app as backend
    target = path or document_path()
    backend.write_json_atomic(target, {kind: sorted(document[kind], key=lambda item: str(item.get('name')))
                                       for kind in KINDS})
