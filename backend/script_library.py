"""剧本库：草稿只在本机，归档才进素材库。

* 草稿存在 ``<数据根>/config/script-library.json``，不进提示词库、不进素材库；
* 归档 = 把正文写成 ``素材库/剧本/日期_标题.md``，已归档列表以这个目录为准；
* 导出由前端直接下载正文，这里只负责存取。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MAX_DRAFTS = 200
MAX_TITLE = 80
MAX_CONTENT = 200_000
MAX_ITEMS = 200
ITEM_TYPES = {'character': '角色', 'scene': '场景', 'prop': '道具',
              'voice': '音色', 'music': '配乐'}
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOCK = threading.Lock()


def document_path() -> Path:
    from . import app as backend
    return Path(backend.ROOT) / 'config' / 'script-library.json'


def archive_dir() -> Path:
    from . import app as backend
    return Path(backend.ASSETS_ROOT) / '剧本'


def _safe(value: str, fallback: str) -> str:
    label = _SAFE.sub('_', value).strip(' .')
    return label[:64].rstrip(' .') or fallback


def _clean(title: Any, content: Any) -> tuple[str, str]:
    label = str(title or '').strip()
    text = str(content or '')
    if not label:
        raise ValueError('剧本需要一个标题')
    if len(label) > MAX_TITLE:
        raise ValueError(f'标题最长 {MAX_TITLE} 个字符')
    if len(text) > MAX_CONTENT:
        raise ValueError('正文超过 20 万字符，请拆分后再存')
    return label, text


def load_drafts(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or document_path()
    try:
        document = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法读取剧本库: {exc}') from exc
    drafts = document.get('drafts') if isinstance(document, dict) else None
    if not isinstance(drafts, list):
        raise ValueError('剧本库文件格式无效')
    return [dict(item) for item in drafts if isinstance(item, dict) and item.get('id')]


def _write(drafts: list[dict[str, Any]], path: Path | None) -> None:
    from . import app as backend
    backend.write_json_atomic(path or document_path(), {'schema_version': 1, 'drafts': drafts})


def list_archived() -> list[dict[str, Any]]:
    folder = archive_dir()
    if not folder.is_dir():
        return []
    files = [item for item in folder.iterdir() if item.is_file() and item.suffix.lower() in {'.md', '.txt'}]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [{'name': item.name, 'path': '素材库/剧本/' + item.name, 'size': item.stat().st_size,
             'modified': item.stat().st_mtime} for item in files]


def listing(path: Path | None = None) -> dict[str, Any]:
    drafts = sorted(load_drafts(path), key=lambda item: float(item.get('updated_at') or 0), reverse=True)
    def summary(item: dict[str, Any]) -> dict[str, Any]:
        breakdown = item.get('breakdown') if isinstance(item.get('breakdown'), dict) else {}
        return {'id': item.get('id'), 'title': item.get('title'), 'content': item.get('content'),
                'updated_at': item.get('updated_at'),
                # 列表只带计数：完整清单走草稿详情，避免每次刷新都把几百条描述拖下来。
                'items': len(breakdown.get('items') or []),
                'confirmed': bool(breakdown.get('confirmed'))}
    return {'drafts': [summary(item) for item in drafts], 'archived': list_archived()}


def create_draft(title: Any, content: Any, *, path: Path | None = None) -> dict[str, Any]:
    label, text = _clean(title, content)
    with _LOCK:
        drafts = load_drafts(path)
        if len(drafts) >= MAX_DRAFTS:
            raise ValueError(f'草稿最多 {MAX_DRAFTS} 份，请先归档或删除')
        entry = {'id': uuid.uuid4().hex, 'title': label, 'content': text, 'updated_at': time.time()}
        _write([*drafts, entry], path)
    return entry


def update_draft(draft_id: str, title: Any, content: Any, *, path: Path | None = None) -> dict[str, Any]:
    label, text = _clean(title, content)
    with _LOCK:
        drafts = load_drafts(path)
        entry = next((item for item in drafts if item.get('id') == draft_id), None)
        if entry is None:
            raise ValueError('草稿不存在，可能已被归档或删除')
        entry.update(title=label, content=text, updated_at=time.time())
        _write(drafts, path)
    return entry


def _clip(value: Any, limit: int) -> str:
    return str(value or '').strip()[:limit]


def public_draft(entry: dict[str, Any]) -> dict[str, Any]:
    """草稿详情：带上素材清单，并标出正文在拆解后有没有被改过。"""
    breakdown = entry.get('breakdown') if isinstance(entry.get('breakdown'), dict) else None
    source_hash = str((breakdown or {}).get('source_hash') or '')
    content = str(entry.get('content') or '')
    stale = bool(source_hash) and source_hash != hashlib.sha1(content.encode('utf-8')).hexdigest()
    return {'id': entry.get('id'), 'title': entry.get('title'), 'content': content,
            'updated_at': entry.get('updated_at'),
            'breakdown': breakdown or {'items': [], 'confirmed': False},
            'stale': stale}


def get_draft(draft_id: str, *, path: Path | None = None) -> dict[str, Any]:
    entry = next((item for item in load_drafts(path) if item.get('id') == draft_id), None)
    if entry is None:
        raise ValueError('草稿不存在，可能已被归档或删除')
    return entry


def normalize_items(items: Any) -> list[dict[str, Any]]:
    """把界面/模型给的条目收敛成干净结构：类型合法、长度受限、同类型同名去重。"""
    if not isinstance(items, list):
        raise ValueError('清单必须是数组')
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in items[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get('type') or '')
        if kind not in ITEM_TYPES:
            continue
        title = _clip(raw.get('title'), 80)
        if not title:
            continue
        key = (kind, title.casefold())
        if key in seen:
            continue
        seen.add(key)
        enabled = raw.get('enabled', True)
        normalized.append({
            'id': _clip(raw.get('id'), 40) or f'it-{uuid.uuid4().hex[:10]}',
            'type': kind, 'title': title,
            'prompt': _clip(raw.get('prompt'), 1200),
            # 声线只在角色/音色两类上有意义，别的条目留着只会让界面多一个空框。
            'voice': _clip(raw.get('voice'), 600) if kind in ('character', 'voice') else '',
            'note': _clip(raw.get('note'), 200),
            'enabled': enabled is not False,
        })
    return normalized


def _named(rows: Any, kind: str, *, prompt_key: str = 'look',
           extra_key: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        name = _clip(raw.get('name'), 60)
        if not name:
            continue
        items.append({'type': kind, 'title': name,
                      'prompt': _clip(raw.get(prompt_key), 1200),
                      'note': _clip(raw.get(extra_key), 200) if extra_key else ''})
    return items


def items_from_extraction(payload: Any) -> list[dict[str, Any]]:
    """把模型的分类 JSON 摊平成一条条可勾选的素材。

    角色描述里如果写了声线，就额外派生一条「某角色 · 声线」——
    它在生成阶段对应音色设计，和出图不是同一条链路。
    """
    if not isinstance(payload, dict):
        raise ValueError('模型返回的不是一个对象')
    items: list[dict[str, Any]] = []
    for raw in payload.get('characters') if isinstance(payload.get('characters'), list) else []:
        if not isinstance(raw, dict):
            continue
        name = _clip(raw.get('name'), 60)
        if not name:
            continue
        items.append({'type': 'character', 'title': name, 'prompt': _clip(raw.get('look'), 1200)})
        voice = _clip(raw.get('voice'), 600)
        if voice:
            items.append({'type': 'voice', 'title': f'{name} · 声线', 'prompt': voice,
                          'note': f'从角色「{name}」派生'})
    items.extend(_named(payload.get('scenes'), 'scene'))
    items.extend(_named(payload.get('props'), 'prop'))
    for raw in payload.get('music') if isinstance(payload.get('music'), list) else []:
        if not isinstance(raw, dict):
            continue
        name = _clip(raw.get('name'), 60) or '配乐'
        seconds = raw.get('seconds')
        note = f"建议 {int(seconds)} 秒" if type(seconds) in (int, float) and 2 <= seconds <= 600 else ''
        items.append({'type': 'music', 'title': name, 'prompt': _clip(raw.get('style'), 1200), 'note': note})
    return normalize_items(items)


def parse_breakdown_json(text: str) -> dict[str, Any]:
    """模型偶尔会把 JSON 包在 ``` 里或前后加一句话，这里只取最外层对象。"""
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[A-Za-z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('模型没有返回可解析的 JSON 清单')
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise ValueError(f'清单 JSON 解析失败：{exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('清单不是对象格式')
    return parsed


def save_breakdown(draft_id: str, items: Any, *, confirmed: bool = False,
                   meta: dict[str, Any] | None = None, path: Path | None = None) -> dict[str, Any]:
    normalized = normalize_items(items)
    with _LOCK:
        drafts = load_drafts(path)
        entry = next((item for item in drafts if item.get('id') == draft_id), None)
        if entry is None:
            raise ValueError('草稿不存在，可能已被归档或删除')
        previous = entry.get('breakdown') if isinstance(entry.get('breakdown'), dict) else {}
        breakdown = {**previous, **(meta or {}), 'items': normalized,
                     'confirmed': bool(confirmed), 'updated_at': time.time()}
        entry['breakdown'] = breakdown
        entry['updated_at'] = time.time()
        _write(drafts, path)
    return public_draft(entry)


def remove_draft(draft_id: str, *, path: Path | None = None) -> bool:
    with _LOCK:
        drafts = load_drafts(path)
        others = [item for item in drafts if item.get('id') != draft_id]
        if len(others) == len(drafts):
            return False
        _write(others, path)
    return True


def remove_archived(name: str) -> dict[str, Any]:
    """删掉一份已归档剧本；归档区是它唯一的位置，删了没有第二份。

    只接受纯文件名，带目录成分或非 .md/.txt 一律拒绝，避免删到剧本目录之外。
    """
    folder = archive_dir().resolve()
    candidate = Path(str(name))
    if candidate.name != str(name) or candidate.suffix.lower() not in {'.md', '.txt'}:
        raise ValueError('只接受剧本库里的文件名')
    path = (folder / candidate.name).resolve()
    if path.parent != folder or not path.is_file():
        raise ValueError('剧本库里没有这个文件')
    size = path.stat().st_size
    path.unlink()
    return {'deleted': '素材库/剧本/' + path.name, 'name': path.name, 'size': size}


def restore_archived(name: str, *, path: Path | None = None) -> dict[str, Any]:
    """把一份已归档剧本收回草稿。

    手动放进 ``素材库/剧本`` 的 .md/.txt 也会被列成「已归档」，所以这里不区分来源，
    只认纯文件名：正文落成一份草稿，归档文件移进回收站（可还原），
    避免同一份剧本在草稿区和归档区各留一份。
    """
    folder = archive_dir().resolve()
    candidate = Path(str(name))
    if candidate.name != str(name) or candidate.suffix.lower() not in {'.md', '.txt'}:
        raise ValueError('只接受剧本库里的文件名')
    source = (folder / candidate.name).resolve()
    if source.parent != folder or not source.is_file():
        raise ValueError('剧本库里没有这个文件')
    try:
        text = source.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'无法读取这份剧本：{exc}') from exc
    # 归档文件名形如 2026-11-12_雨夜独白.md；去掉日期前缀当标题，手动放的文件原样用。
    label = re.sub(r'^\d{4}-\d{2}-\d{2}[_-]', '', candidate.stem).strip() or '收回的剧本'
    entry = create_draft(label[:MAX_TITLE], text, path=path)
    from . import trash
    recycled, failed = trash.recycle([source])
    if failed:
        # 文件没移走就别留一份重复草稿：撤掉刚建的，让用户稍后重试。
        remove_draft(str(entry['id']), path=path)
        raise ValueError('归档文件正被占用，暂时没能移走；请稍后再试')
    return {'draft': entry, 'name': candidate.name, 'recycled': True}


def archive_draft(draft_id: str, *, path: Path | None = None) -> dict[str, Any]:
    with _LOCK:
        drafts = load_drafts(path)
        entry = next((item for item in drafts if item.get('id') == draft_id), None)
        if entry is None:
            raise ValueError('草稿不存在，可能已经归档')
        folder = archive_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stem = _safe(str(entry['title']), '剧本')
        destination = folder / f'{time.strftime("%Y-%m-%d")}_{stem}.md'
        counter = 2
        while destination.exists():
            destination = folder / f'{time.strftime("%Y-%m-%d")}_{stem}-{counter}.md'
            counter += 1
        destination.write_text(str(entry.get('content') or ''), encoding='utf-8')
        _write([item for item in drafts if item.get('id') != draft_id], path)
    return {'name': destination.name, 'path': '素材库/剧本/' + destination.name,
            'size': destination.stat().st_size, 'title': entry['title']}
