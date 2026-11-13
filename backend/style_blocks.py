"""风格锁块：不改用户写的提示词，只在「送进模型的那一份」前面加一段固定约束。

来源是 manju-laoli-skill 的 ``style-prefix-blocks.md``（MIT）。原块是给全片用的，
这里按单图 / 单段压缩到 150 字上下，只保留成像、光比、光学、材质与针对性禁项。

约定：
- ``request.prompt`` 永远是用户原文，一个字符都不动；
- ``prompt_for_model`` / ``prompt_override`` 才拼接规则块，任务记录里能看到套了哪块；
- 不选（None / 空串）时原样返回，行为与以前完全一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BLOCK_PATH = Path(__file__).resolve().parent.parent / 'contracts' / 'style-blocks.json'


_CACHE: dict[str, Any] = {'mtime': None, 'data': {'blocks': []}}


def load() -> dict[str, Any]:
    """按文件修改时间缓存：改规则文字不用重启服务，下次读取就生效。"""
    try:
        stamp = BLOCK_PATH.stat().st_mtime
    except OSError:
        return _CACHE['data']
    if _CACHE['mtime'] == stamp:
        return _CACHE['data']
    try:
        document = json.loads(BLOCK_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _CACHE['data']
    blocks = document.get('blocks')
    _CACHE['data'] = {'blocks': blocks if isinstance(blocks, list) else []}
    _CACHE['mtime'] = stamp
    return _CACHE['data']


def catalog() -> list[dict[str, str]]:
    """给界面用：只回 id / name / note，不回正文（正文是给模型的，界面上没必要摊开）。"""
    out: list[dict[str, str]] = []
    for item in load()['blocks']:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get('id') or '').strip()
        if not block_id:
            continue
        out.append({'id': block_id, 'name': str(item.get('name') or block_id),
                    'note': str(item.get('note') or '')})
    return out


def find(block_id: str | None) -> dict[str, Any] | None:
    key = str(block_id or '').strip()
    if not key:
        return None
    return next((item for item in load()['blocks']
                 if isinstance(item, dict) and str(item.get('id') or '') == key), None)


def apply(prompt: str, block_id: str | None) -> tuple[str, dict[str, str] | None]:
    """返回（真正送进模型的提示词，套用记录）。

    套用记录写进任务 payload，方便回看时知道这一张为什么长这样；
    不套用时第二项是 None，调用方保持原字段不变。
    """
    block = find(block_id)
    text = str(prompt or '')
    if not block:
        return text, None
    body = str(block.get('text') or '').strip()
    if not body:
        return text, None
    record = {'id': str(block.get('id') or ''), 'name': str(block.get('name') or '')}
    if not text.strip():
        return body, record
    return f'{body}\n{text}', record
