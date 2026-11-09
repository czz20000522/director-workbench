"""只读扫描：ComfyUI 输出/输入目录里「任务删除碰不到」的文件。

单步任务删除按任务记录清理产物，所以这些内容会留在磁盘上：

* 直接打 ComfyUI 生成的实验图（从不进工作台任务表）；
* 任务记录已删、但产物当时被别的任务引用而保留下来的文件；
* 手动放进 ``input/`` 的参考素材。

本模块**只出清单，绝不删任何东西**；是否清理、怎么清理由用户另行决定。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import library_archive

# 引擎自带的元数据/日志不算生成产物，列出来只会淹没真正想看的条目。
SKIP_SUFFIXES = {'.json', '.txt', '.log', '.md'}
TASK_ID_HEAD = re.compile(r'^[0-9a-f]{8}-')


def _reason(path: Path, referenced: set[str], task_heads: set[str], input_root: Path) -> tuple[str, str] | None:
    key = str(path)
    if key in referenced:
        return None
    if path.is_relative_to(input_root):
        return 'input_material', '手动放入或缓存的参考素材，不属于任何生成任务'
    if TASK_ID_HEAD.match(path.name) and path.name[:8] not in task_heads:
        return 'task_missing', '文件名带任务 ID，但该任务记录已不存在'
    return 'unreferenced', '没有任何任务记录引用这个文件'


def scan() -> dict[str, Any]:
    from . import app as backend
    output_root = backend.OUTPUT_ROOT.resolve()
    input_root = backend.INPUT_ROOT.resolve()
    roots = [output_root, input_root]
    protected = [library_archive.assets_root().resolve(),
                 library_archive.works_archive_root().resolve()]

    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT id, payload, result FROM tasks').fetchall()
    task_heads = {str(row[0])[:8] for row in rows}
    referenced: set[str] = set()
    for _task_id, payload, result in rows:
        body = library_archive._loads(payload)
        outcome = library_archive._loads(result)
        for candidate in library_archive._output_candidates(body, outcome):
            referenced.add(str(candidate))

    items: list[dict[str, Any]] = []
    for root in roots:
        for file in root.rglob('*'):
            if not file.is_file() or file.suffix.lower() in SKIP_SUFFIXES:
                continue
            resolved = file.resolve()
            if any(resolved.is_relative_to(item) for item in protected):
                continue
            verdict = _reason(resolved, referenced, task_heads, input_root)
            if verdict is None:
                continue
            reason, label = verdict
            try:
                stat = resolved.stat()
            except OSError:
                continue
            items.append({
                'path': str(resolved), 'name': resolved.name,
                'size': stat.st_size, 'modified_at': stat.st_mtime,
                'root': 'output' if resolved.is_relative_to(output_root) else 'input',
                'reason': reason, 'label': label,
            })
    items.sort(key=lambda item: item['modified_at'], reverse=True)
    by_reason: dict[str, int] = {}
    for item in items:
        by_reason[item['reason']] = by_reason.get(item['reason'], 0) + 1
    return {
        'generated_at': time.time(),
        'roots': [str(root) for root in roots],
        'items': items, 'count': len(items),
        'total_bytes': sum(item['size'] for item in items),
        'by_reason': by_reason,
        'note': '只读清单：本接口不会删除、移动或改名任何文件。',
    }
