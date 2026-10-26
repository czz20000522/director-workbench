"""删除前的可选归档，以及「连带清除」到底删哪些文件。

两条硬规矩：
* 归档只搬用户明确勾选的那几个文件，进 ``素材库/<库名>/`` 并登记成库条目；
* 连带清除只删**属于这些任务/作品**的产物——路径里必须带任务 ID 或作品 ID，
  且不被其它任务引用。共享目录里的别的东西一律不动。
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import preset_library

TARGETS = {'reference': '参考图', 'timbre': '音色', 'music': '配乐', 'film': '作品'}
TIMBRE_SUBTITLE = '音色样本 · 待试听'
MUSIC_SUBTITLE = '配乐候选 · 待审核'
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def assets_root() -> Path:
    from . import app as backend
    return Path(backend.ASSETS_ROOT)


def works_archive_root() -> Path:
    from . import app as backend
    return Path(backend.WORKS_ARCHIVE_ROOT)


def safe_name(value: str, fallback: str) -> str:
    label = _SAFE.sub('_', value).strip(' .')
    return (label[:48].rstrip(' .') or fallback)


def _manifest(project_id: str) -> dict[str, Any]:
    from . import app as backend
    return backend.require_project_manifest(project_id)


def candidates(project_id: str, task_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Files that can be kept in a library. Nothing here is archived by default."""
    manifest = _manifest(project_id)
    items = []
    for asset in manifest.get('assets') or []:
        if not isinstance(asset, dict):
            continue
        if task_ids is not None:
            owner = asset.get('speech_task_id') or asset.get('keyframe_task_id') or asset.get('audio_swap_task_id')
            if owner not in task_ids:
                continue
        # 生成的候选记在 sources.A，登记/上传的素材只带 image_path。
        source = (asset.get('sources') or {}).get('A') or asset.get('image_path') or asset.get('path')
        if not isinstance(source, str) or not source:
            continue
        if asset.get('kind') == 'image':
            target = 'reference'
        elif asset.get('kind') == 'audio' and asset.get('subtitle') == TIMBRE_SUBTITLE:
            target = 'timbre'
        elif asset.get('kind') == 'audio' and asset.get('subtitle') == MUSIC_SUBTITLE:
            target = 'music'
        elif asset.get('kind') == 'video':
            target = 'film'
        else:
            continue
        items.append({'asset_id': asset.get('id'), 'project_id': project_id,
                      'project_title': str(manifest.get('title') or project_id),
                      'kind': asset.get('kind'), 'target': target,
                      'origin': str(asset.get('origin') or ''),
                      'library': TARGETS[target], 'title': str(asset.get('title') or asset.get('id')),
                      'source': source})
    return items


def _resolve(source: str) -> Path:
    from . import app as backend
    path = backend.resolve_registered_path(source)
    if not path.is_file():
        raise ValueError(f'归档来源已不存在: {source}')
    return path


ORIGIN_ENGINES = {'VoxCPM2': 'voxcpm-2', 'IndexTTS 2.5': 'indextts-2.5'}


def _register(target: str, name: str, path: Path, origin: str = '') -> None:
    from . import app as backend
    if target == 'timbre':
        relative = '素材库/' + '/'.join(path.relative_to(assets_root()).parts)
        params = {'engine': 'indextts-2.5', 'voice_reference': relative}
        # 样本是哪台引擎做的要记下来；归档只是把它变成可复用的参考。
        generated_by = ORIGIN_ENGINES.get(origin)
        if generated_by:
            params['generated_by'] = generated_by
        preset_library.save('timbre', name, params)
        return
    if target == 'reference':
        config = Path(backend.ROOT) / 'config' / 'reference-library.json'
        try:
            document = json.loads(config.read_text(encoding='utf-8'))
        except FileNotFoundError:
            document = {'schema_version': 1, 'documents': [], 'image_directories': []}
        directories = document.setdefault('image_directories', [])
        folder = str(path.parent.resolve())
        if not any(isinstance(item, dict) and str(item.get('path')) == folder for item in directories):
            directories.append({'title': f'归档参考图 · {path.parent.name}', 'work': '归档素材', 'path': folder})
            backend.write_json_atomic(config, document)
        return
    raise ValueError('未知归档库')


def _stamp(now: float | None = None) -> str:
    import time as _time
    return _time.strftime('%Y-%m-%d', _time.localtime(now))


def archive(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move the chosen files into their library, named by date and title.

    归档即搬离临时作品目录：删除作品时素材库与作品归档根都不在删除范围内。
    """
    done = []
    stamp = _stamp()
    for item in items:
        source = _resolve(str(item['source']))
        target = str(item['target'])
        if target not in TARGETS:
            raise ValueError('未知归档库')
        title = safe_name(str(item.get('title') or source.stem), uuid.uuid4().hex[:8])
        if target == 'film':
            work = safe_name(str(item.get('project_title') or '未命名作品'), '未命名作品')
            folder = works_archive_root() / f'{stamp}_{work}'
        else:
            folder = assets_root() / TARGETS[target]
        folder.mkdir(parents=True, exist_ok=True)
        name = f'{stamp}_{title}'
        destination = folder / f'{name}{source.suffix.lower()}'
        counter = 2
        while destination.exists():
            destination = folder / f'{name}-{counter}{source.suffix.lower()}'
            counter += 1
        shutil.move(str(source), str(destination))
        if target in ('timbre', 'reference'):
            _register(target, name, destination, str(item.get('origin') or ''))
        done.append({'asset_id': item.get('asset_id'), 'target': target, 'library': TARGETS[target],
                     'name': name, 'path': str(destination)})
    return done


def owned_outputs(rows: list[dict[str, Any]]) -> list[str]:
    """Outputs of these tasks that live outside the workspace, frozen for the preview.

    A file qualifies only when its name carries one of the task ids and no other
    task references the same path — shared outputs never fall into this net.
    """
    from . import app as backend
    roots = [backend.OUTPUT_ROOT.resolve(), backend.INPUT_ROOT.resolve()]
    wanted = {str(row.get('id')) for row in rows if row.get('id')}
    if not wanted:
        return []
    others = []
    with backend.DB_LOCK, backend.db() as connection:
        for (payload,) in connection.execute('SELECT payload FROM tasks').fetchall():
            try:
                document = json.loads(payload)
            except ValueError:
                continue
            if str(document.get('task_id') or '') in wanted:
                continue
            others.append(payload)
    others_text = '\n'.join(others)
    found: list[str] = []
    for row in rows:
        payload = row.get('payload') or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        for value in _strings(payload):
            candidate = Path(value)
            if not candidate.is_absolute():
                continue
            resolved = candidate.resolve()
            if not any(resolved.is_relative_to(root) for root in roots):
                continue
            if not any(task in resolved.name for task in wanted):
                continue
            if value in others_text:
                continue
            if resolved.is_file() and str(resolved) not in found:
                found.append(str(resolved))
    return found


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def task_rows(project_id: str | None = None, task_ids: list[str] | None = None) -> list[dict[str, Any]]:
    from . import app as backend
    with backend.DB_LOCK, backend.db() as connection:
        if task_ids:
            marks = ','.join('?' * len(task_ids))
            rows = connection.execute(f'SELECT * FROM tasks WHERE id IN ({marks})', tuple(task_ids)).fetchall()
        else:
            rows = connection.execute('SELECT * FROM tasks WHERE project_id = ?', (project_id,)).fetchall()
    return [backend.task_row(row) for row in rows if row]


def prune_assets(project_id: str, asset_ids: set[str]) -> list[str]:
    """Drop library/deleted candidates from the manifest so no dead entries remain."""
    from . import app as backend
    if not asset_ids:
        return []
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(project_id)
        before = [asset for asset in (manifest.get('assets') or []) if isinstance(asset, dict)]
        after = [asset for asset in before if asset.get('id') not in asset_ids]
        removed = [str(asset.get('id')) for asset in before if asset.get('id') in asset_ids]
        if removed:
            manifest['assets'] = after
            backend.save_project_manifest(manifest)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
    return removed


def delete_files(paths: list[str]) -> list[str]:
    """Delete only files under the configured ComfyUI roots; ignore everything else.

    素材库与作品归档根是归档区：即便有人把指向它们的路径塞进来，也一律跳过。
    """
    from . import app as backend
    roots = [backend.OUTPUT_ROOT.resolve(), backend.INPUT_ROOT.resolve()]
    protected = [assets_root().resolve(), works_archive_root().resolve()]
    removed = []
    for value in paths:
        path = Path(str(value))
        if not path.is_absolute():
            continue
        resolved = path.resolve()
        if any(resolved.is_relative_to(root) for root in protected):
            continue
        if not any(resolved.is_relative_to(root) for root in roots) or not resolved.is_file():
            continue
        resolved.unlink(missing_ok=True)
        removed.append(str(resolved))
    return removed
