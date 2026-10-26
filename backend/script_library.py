"""剧本库：草稿只在本机，归档才进素材库。

* 草稿存在 ``<数据根>/config/script-library.json``，不进提示词库、不进素材库；
* 归档 = 把正文写成 ``素材库/剧本/日期_标题.md``，已归档列表以这个目录为准；
* 导出由前端直接下载正文，这里只负责存取。
"""
from __future__ import annotations

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
    return {'drafts': [{key: item.get(key) for key in ('id', 'title', 'content', 'updated_at')} for item in drafts],
            'archived': list_archived()}


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


def remove_draft(draft_id: str, *, path: Path | None = None) -> bool:
    with _LOCK:
        drafts = load_drafts(path)
        others = [item for item in drafts if item.get('id') != draft_id]
        if len(others) == len(drafts):
            return False
        _write(others, path)
    return True


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
