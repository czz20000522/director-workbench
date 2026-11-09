"""提示词库：把写好的画面描述存成可复用的条目，并记住它是怎么来的。

存的不是裸字符串，而是「词 + 上下文」：哪个作品、哪个制作方案、画质档、画幅、
种子、用了哪几张参考素材、最后产出了哪张图。下次要用，可以只抄文本，也可以
连参数一起填回工作台——这样一条提示词就不只是句子，而是一份可复现的配方。

只写本机数据根（``director-data/config/prompt-library.json``），不联网、不进仓库。
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

MAX_ITEMS = 500
MAX_TEXT = 2000
MAX_TITLE = 60
MAX_TAGS = 8
MAX_TAG = 16
MAX_NOTE = 300
TITLE_LENGTH = 24
_LOCK = threading.Lock()

LINK_KEYS = ('project_id', 'preset_id', 'profile', 'aspect', 'seed', 'materials', 'result', 'task_id')


def document_path() -> Path:
    # 调用时再取 ROOT：测试和私有布局都会重定向工作台根目录。
    from . import app as backend
    return Path(backend.ROOT) / 'config' / 'prompt-library.json'


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('提示词必须是文字')
    text = value.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        raise ValueError('提示词不能为空')
    if len(text) > MAX_TEXT:
        raise ValueError(f'提示词太长，控制在 {MAX_TEXT} 字以内')
    return text


def auto_title(text: str) -> str:
    """取第一句当标题：整段塞进列表会挤成一团。"""
    head = re.split(r'[。！？；\n]', text.strip(), maxsplit=1)[0].strip()
    head = head or text.strip()
    return head[:TITLE_LENGTH]


def _clean_title(value: Any, text: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return auto_title(text)
    if not isinstance(value, str):
        raise ValueError('标题必须是文字')
    label = ''.join(char for char in value if unicodedata.category(char)[0] != 'C' and char not in '<>').strip()
    if not label:
        return auto_title(text)
    return label[:MAX_TITLE]


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('标签必须是列表')
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError('标签必须是文字')
        tag = item.strip().lstrip('#')
        if not tag:
            continue
        if len(tag) > MAX_TAG:
            raise ValueError(f'单个标签不超过 {MAX_TAG} 字')
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f'最多 {MAX_TAGS} 个标签')
    return tags


def _clean_links(value: Any) -> dict[str, Any]:
    """只认白名单字段：库里存的必须能安全回填到请求里。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('关联信息必须是 JSON 对象')
    unknown = set(value) - set(LINK_KEYS)
    if unknown:
        raise ValueError(f'关联信息里有未知字段：{"、".join(sorted(unknown))}')
    links: dict[str, Any] = {}
    for key in ('project_id', 'preset_id', 'profile', 'aspect', 'task_id', 'result'):
        raw = value.get(key)
        if raw in (None, ''):
            continue
        if not isinstance(raw, str):
            raise ValueError(f'关联信息 {key} 必须是文字')
        links[key] = raw.strip()[:1000]
    seed = value.get('seed')
    if seed is not None:
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError('关联信息的种子须为 32 位非负整数')
        links['seed'] = seed
    materials = value.get('materials')
    if materials is not None:
        if not isinstance(materials, list):
            raise ValueError('关联素材必须是列表')
        cleaned: list[dict[str, str]] = []
        for item in materials[:8]:
            if not isinstance(item, dict):
                raise ValueError('关联素材条目必须是对象')
            role = str(item.get('role') or '').strip()[:16]
            label = str(item.get('label') or '').strip()[:80]
            material_id = str(item.get('material_id') or '').strip()[:80]
            if not (role or label or material_id):
                continue
            cleaned.append({'role': role, 'label': label, 'material_id': material_id})
        if cleaned:
            links['materials'] = cleaned
    return links


def _clean_note(value: Any) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('备注必须是文字')
    note = value.strip()
    if len(note) > MAX_NOTE:
        raise ValueError(f'备注不超过 {MAX_NOTE} 字')
    return note


def load(path: Path | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {'version': 1, 'items': []}
    target = path or document_path()
    try:
        stored = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return document
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法读取提示词库: {exc}') from exc
    if not isinstance(stored, dict):
        raise ValueError('提示词库文件不是 JSON 对象')
    items = stored.get('items', [])
    if not isinstance(items, list):
        raise ValueError('提示词库的 items 不是列表')
    document['items'] = [dict(item) for item in items if isinstance(item, dict) and item.get('id')]
    return document


def _write(document: dict[str, Any], path: Path | None) -> None:
    from . import app as backend
    target = path or document_path()
    backend.write_json_atomic(target, {'version': 1, 'items': document['items']})


def _merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for key, value in new.items():
        if key in ('materials',) and not value:
            continue
        if value in (None, '', [], {}):
            continue
        merged[key] = value
    return merged


def create(text: str, *, title: Any = None, tags: Any = None, source: str = 'manual',
           idea: str = '', model: str = '', note: Any = None, links: Any = None,
           path: Path | None = None, now: int | None = None) -> dict[str, Any]:
    """存一条。同文只留一条：重复保存会把关联信息合并过去，而不是刷出一堆副本。"""
    body = _clean_text(text)
    if source not in ('manual', 'draft', 'generation', 'import'):
        raise ValueError('未知的提示词来源')
    label = _clean_title(title, body)
    tag_list = _clean_tags(tags)
    link_map = _clean_links(links)
    stamp = now if isinstance(now, int) else _now_ms()
    with _LOCK:
        document = load(path)
        existing = next((item for item in document['items'] if item.get('text') == body), None)
        if existing is not None:
            merged_tags = list(dict.fromkeys([*(existing.get('tags') or []), *tag_list]))[:MAX_TAGS]
            existing.update({
                'title': label if title else existing.get('title') or label,
                'tags': merged_tags,
                'links': _merge(existing.get('links') or {}, link_map),
                'updated_at': stamp,
            })
            if note is not None:
                existing['note'] = _clean_note(note)
            if idea and not existing.get('idea'):
                existing['idea'] = str(idea).strip()[:MAX_TEXT]
            _write(document, path)
            return {'item': existing, 'created': False}
        if len(document['items']) >= MAX_ITEMS:
            raise ValueError(f'提示词库已满（{MAX_ITEMS} 条），先删几条再存')
        item = {
            'id': f'pl_{stamp:x}_{len(document["items"]):03d}',
            'text': body,
            'title': label,
            'tags': tag_list,
            'note': _clean_note(note),
            'source': source,
            'idea': str(idea or '').strip()[:MAX_TEXT],
            'model': str(model or '').strip()[:80],
            'links': link_map,
            'uses': 0,
            'created_at': stamp,
            'updated_at': stamp,
        }
        document['items'].append(item)
        _write(document, path)
        return {'item': item, 'created': True}


def update(item_id: str, *, title: Any = None, tags: Any = None, note: Any = None,
           path: Path | None = None, now: int | None = None) -> dict[str, Any]:
    with _LOCK:
        document = load(path)
        item = next((entry for entry in document['items'] if entry.get('id') == item_id), None)
        if item is None:
            raise KeyError('提示词库里没有这一条')
        if title is not None:
            item['title'] = _clean_title(title, str(item.get('text') or ''))
        if tags is not None:
            item['tags'] = _clean_tags(tags)
        if note is not None:
            item['note'] = _clean_note(note)
        item['updated_at'] = now if isinstance(now, int) else _now_ms()
        _write(document, path)
        return item


def remove(item_id: str, path: Path | None = None) -> bool:
    with _LOCK:
        document = load(path)
        kept = [item for item in document['items'] if item.get('id') != item_id]
        if len(kept) == len(document['items']):
            return False
        document['items'] = kept
        _write(document, path)
        return True


def mark_used(item_id: str, *, result: str = '', task_id: str = '',
              path: Path | None = None, now: int | None = None) -> dict[str, Any]:
    """记一次使用。带上产物路径，就能回头知道这条词最后出了什么。"""
    with _LOCK:
        document = load(path)
        item = next((entry for entry in document['items'] if entry.get('id') == item_id), None)
        if item is None:
            raise KeyError('提示词库里没有这一条')
        item['uses'] = int(item.get('uses') or 0) + 1
        item['last_used_at'] = now if isinstance(now, int) else _now_ms()
        links = dict(item.get('links') or {})
        if result:
            links['result'] = str(result).strip()[:1000]
        if task_id:
            links['task_id'] = str(task_id).strip()[:1000]
        item['links'] = links
        _write(document, path)
        return item


def _haystack(item: dict[str, Any]) -> str:
    links = item.get('links') or {}
    parts = [str(item.get('title') or ''), str(item.get('text') or ''), str(item.get('note') or ''),
             str(item.get('idea') or ''), ' '.join(item.get('tags') or []),
             str(links.get('profile') or ''), str(links.get('preset_id') or ''),
             ' '.join(str(material.get('label') or '') for material in links.get('materials') or [])]
    return '\n'.join(parts).casefold()


def search(*, query: str = '', tag: str = '', project_id: str = '',
           path: Path | None = None) -> dict[str, Any]:
    document = load(path)
    needle = str(query or '').strip().casefold()
    wanted_tag = str(tag or '').strip().lstrip('#')
    target_project = str(project_id or '').strip()
    items = document['items']
    if target_project:
        items = [item for item in items
                 if str((item.get('links') or {}).get('project_id') or '') in ('', target_project)]
    if wanted_tag:
        items = [item for item in items if wanted_tag in (item.get('tags') or [])]
    if needle:
        items = [item for item in items if needle in _haystack(item)]
    items = sorted(items, key=lambda item: int(item.get('updated_at') or 0), reverse=True)
    tags: dict[str, int] = {}
    for item in document['items']:
        for value in item.get('tags') or []:
            tags[str(value)] = tags.get(str(value), 0) + 1
    return {'items': items, 'total': len(document['items']), 'matched': len(items),
            'tags': [{'tag': key, 'count': count}
                     for key, count in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))]}
