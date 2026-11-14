"""音色库与配乐库：起点预设卡片，不是你的参数集合。

内置卡片写在这个文件里（可带入，不能删）；你自己存的卡片落在
``<数据根>/config/preset-library.json``，同样不进仓库。两者一起返回，同名时
你的那张覆盖内置那张。「带入」永远是一次显式点击，不会替任何面板填参数。
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
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
    if set(params) - {'engine', 'voice_description', 'voice_reference', 'text', 'generated_by',
                      'sample_path', 'sample_duration_seconds', 'sample_text'}:
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
    # 试听小样：由语音任务成功后回写，也可以在面板里「存入音色库」时带上。
    sample_path = _text(params.get('sample_path'), '小样路径', 1000, False)
    if sample_path:
        frozen['sample_path'] = sample_path
    if params.get('sample_duration_seconds') is not None:
        duration = params.get('sample_duration_seconds')
        if type(duration) not in (int, float) or not math.isfinite(float(duration)) or float(duration) <= 0:
            raise ValueError('小样时长必须是大于 0 的数字')
        frozen['sample_duration_seconds'] = round(float(duration), 3)
    sample_text = _text(params.get('sample_text'), '小样台词', 2000, False)
    if sample_text:
        frozen['sample_text'] = sample_text
    return frozen


def _validate_music(params: dict[str, Any]) -> dict[str, Any]:
    # sample_path / sample_duration_seconds：这段成品音频存进库之后挂在这张卡上，
    # 删掉原任务也不影响（文件已经复制到预置库目录里）。
    if set(params) - {'tags', 'bpm', 'keyscale', 'timesignature', 'duration_seconds', 'language',
                      'sample_path', 'sample_duration_seconds'}:
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
    frozen = {'tags': tags, 'bpm': bpm, 'keyscale': keyscale, 'timesignature': timesignature,
              'duration_seconds': float(duration), 'language': language}
    sample = _text(params.get('sample_path'), '成品音频', 1000, False)
    if sample:
        frozen['sample_path'] = sample
        sample_seconds = params.get('sample_duration_seconds')
        if type(sample_seconds) in (int, float) and float(sample_seconds) > 0:
            frozen['sample_duration_seconds'] = round(float(sample_seconds), 3)
    return frozen


def _without_samples(params: dict[str, Any]) -> dict[str, Any]:
    # 小样是「后来附上的试听」，不是用户改了起点参数：只挂小样的内置卡仍算未改，
    # 否则一次批量生成试听会让 11 张内置卡全部显示「（已改）」。
    return {key: value for key, value in params.items() if not str(key).startswith('sample_')}


def cards(kind: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Built-ins first, then the user's own cards; a saved name replaces its built-in."""
    if kind not in KINDS:
        raise ValueError('未知预设分区')
    mine = {str(item.get('name')): item for item in load(path)[kind]}
    merged = []
    for builtin in BUILTIN[kind]:
        name = str(builtin['name'])
        saved = mine.pop(name, None)
        changed = bool(saved) and _without_samples(saved['params']) != _without_samples(builtin['params'])
        merged.append({'name': name, 'params': saved['params'] if saved else builtin['params'],
                       'builtin': True, 'modified': changed,
                       # 这两个字段给「删任务时判断素材是否已归档」用，界面可以忽略。
                       'saved_at': (saved or {}).get('saved_at'),
                       'source_task_id': (saved or {}).get('source_task_id')})
    merged.extend({'name': name, 'params': item['params'], 'builtin': False, 'modified': False,
                   'saved_at': item.get('saved_at'), 'source_task_id': item.get('source_task_id')}
                  for name, item in sorted(mine.items()))
    return merged


def save(kind: str, name: str, params: dict[str, Any], *, now: float | None = None,
         path: Path | None = None, source_task_id: str | None = None) -> dict[str, Any]:
    label, frozen = clean_name(name), validate_params(kind, params)
    # 手动存卡也走一遍复制：卡片只引用素材库里的副本，删掉原任务不会让试听变 404。
    if isinstance(frozen.get('sample_path'), str) and frozen['sample_path'].strip():
        frozen['sample_path'] = _stable_sample_path(
            frozen['sample_path'], label, '音色库' if kind == 'timbre' else '配乐库')
    with _LOCK:
        document = load(path)
        previous = next((item for item in document[kind] if item.get('name') == label), None)
        others = [item for item in document[kind] if item.get('name') != label]
        if len(others) >= MAX_CARDS:
            raise ValueError(f'每个分区最多保存 {MAX_CARDS} 张卡片')
        entry = {'name': label, 'params': frozen, 'saved_at': time.time() if now is None else now}
        # 记下这张卡是从哪条任务来的：删除任务时用它显示「素材已归档」。
        # 改卡时没带来源就保留原来的，避免编辑一次就丢掉关联。
        linked = str(source_task_id or '').strip() or str((previous or {}).get('source_task_id') or '').strip()
        if linked:
            entry['source_task_id'] = linked
        document[kind] = others + [entry]
        _write(document, path)
    return entry


def _stable_sample_path(file_path: str, description: str, library: str = '音色库') -> str:
    """把任务产物复制进素材库，卡片只引用这份副本。

    直接引用 ``speech-tasks/<id>/result/speech.wav`` 的话，用户删掉那条单步任务，
    文件会随任务进回收站，卡片上的播放键就变成 404——这正是「删任务会不会清空音色库」
    要避免的情况。复制失败就退回原路径，不阻断已经成功的配音。
    """
    source = Path(str(file_path))
    if not source.is_file():
        return str(file_path)
    try:
        from . import app as backend
        folder = Path(backend.ASSETS_ROOT) / library
        resolved = source.resolve()
        if resolved.is_relative_to(folder.resolve()):
            return str(resolved)  # 已经在素材库里，不重复复制
        folder.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha1(str(description or resolved.name).encode('utf-8')).hexdigest()[:12]
        target = folder / f'card-{token}{resolved.suffix.lower() or ".wav"}'
        shutil.copy2(resolved, target)
        return str(target.resolve())
    except OSError:
        return str(source.resolve())


def archived_for_task(task_id: str, sample_paths: list[str] | None = None) -> dict[str, Any] | None:
    """这条任务的产物是否已经收进某个预设库。

    两种认定：
    - 卡片记了 ``source_task_id``（新流程，保存时写入）；
    - 卡片的 sample_path 正好是这条任务的产物（老卡片，按路径认）。

    返回 ``{library, name, at}``，没有就返回 None。
    """
    wanted_task = str(task_id or '').strip()
    wanted_paths = {str(item).replace('\\', '/').strip() for item in (sample_paths or []) if item}
    best: dict[str, Any] | None = None
    for kind, label in (('timbre', '音色库'), ('music', '配乐库')):
        for card in cards(kind):
            linked = wanted_task and str(card.get('source_task_id') or '').strip() == wanted_task
            sample = str(card['params'].get('sample_path') or '').replace('\\', '/').strip()
            by_path = bool(sample) and sample in wanted_paths
            if not (linked or by_path):
                continue
            saved_at = float(card.get('saved_at') or 0)
            if best is None or saved_at > float(best.get('at') or 0):
                best = {'library': label, 'name': card.get('name') or '', 'at': saved_at}
    return best


def attach_sample(description: str, *, source_card: str | None, sample_path: str,
                  sample_duration_seconds: float, sample_text: str = '',
                  path: Path | None = None) -> str | None:
    """把一次成功生成的小样写回匹配的音色卡。

    - 带 ``source_card`` 时优先认那张卡；卡不在了才退回按描述精确匹配。
    - 描述相对卡片改过 → 不写回任何卡（那是另一条音色）。
    - 描述没改 → 覆盖该卡的 sample_*；内置卡会存一份同名覆盖，和手动改卡同一套机制。
    """
    wanted = str(description or '').strip()
    file_path = str(sample_path or '').strip()
    if not wanted or not file_path:
        return None
    # 先落一份到素材库再记账：之后删任务不会影响这张卡的试听。
    file_path = _stable_sample_path(file_path, wanted)
    existing = cards('timbre', path)
    target = None
    label = str(source_card or '').strip()
    if label:
        found = next((item for item in existing if item['name'] == label), None)
        if found is not None:
            if str(found['params'].get('voice_description') or '').strip() != wanted:
                return None
            target = found
    if target is None:
        target = next((item for item in existing
                       if str(item['params'].get('voice_description') or '').strip() == wanted), None)
    if target is None:
        return None
    params = dict(target['params'])
    params['sample_path'] = file_path
    if sample_duration_seconds and float(sample_duration_seconds) > 0:
        params['sample_duration_seconds'] = round(float(sample_duration_seconds), 3)
    if str(sample_text or '').strip():
        params['sample_text'] = str(sample_text).strip()
    save('timbre', target['name'], params, path=path)
    return target['name']


def rename(kind: str, name: str, new_name: str, path: Path | None = None) -> dict[str, Any]:
    """Rename one of your own cards. Built-in cards have nothing to rename.

    改过的内置卡在库里另存了一份同名条目，所以它可以改名——改完之后原来那张
    内置卡回到内置参数，你改的那份挂在新名字下。
    """
    if kind not in KINDS:
        raise ValueError('未知预设分区')
    label, target = clean_name(name), clean_name(new_name)
    if label == target:
        return {'name': target, 'previous': label, 'renamed': False}
    with _LOCK:
        document = load(path)
        entry = next((item for item in document[kind] if str(item.get('name')) == label), None)
        if entry is None:
            raise ValueError('没有找到这张卡片；内置卡片不能重命名，想改就同名另存一张你自己的')
        if any(str(item.get('name')) == target for item in document[kind]):
            raise ValueError(f'已经有叫「{target}」的卡片，换个名字或先删掉那张')
        moved = dict(entry)
        moved['name'] = target
        document[kind] = [item for item in document[kind] if str(item.get('name')) != label] + [moved]
        _write(document, path)
    return {'name': target, 'previous': label, 'renamed': True}


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
