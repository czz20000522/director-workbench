"""清晰化库：把 2× 结果沉淀成素材，可选覆盖原件。

两条硬规矩：
* 归档是复制一份进 ``素材库/清晰化``，名字固定带「清晰化」标签，原名和来源时间都留着；
* 覆盖是显式选择：把原素材登记改指向清晰化文件，并**删掉原文件**。不勾就一个字节都不动。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

MEDIA_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.mp4', '.mov', '.webm', '.mkv'}
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TAG = ' · 清晰化'


def directory() -> Path:
    from . import app as backend
    return Path(backend.ASSETS_ROOT) / '清晰化'


def _relative(path: Path) -> str:
    return '素材库/清晰化/' + path.name


def _safe(value: str) -> str:
    label = _SAFE.sub('_', value).strip(' .')
    return label[:64].rstrip(' .') or '清晰化'


def list_files(limit: int = 60) -> list[dict[str, Any]]:
    folder = directory()
    if not folder.is_dir():
        return []
    files = [item for item in folder.iterdir()
             if item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [{'name': item.name, 'path': _relative(item), 'size': item.stat().st_size,
             'modified': item.stat().st_mtime} for item in files[:limit]]


def task_output(task: dict[str, Any]) -> Path:
    from . import app as backend
    result = task.get('result') or {}
    outputs = result.get('published_outputs') or [f"output/{value}" for value in result.get('outputs') or []]
    if len(outputs) != 1:
        raise ValueError('清晰化回执没有唯一输出，请先核对任务')
    path = backend.resolve_registered_path(outputs[0])
    if not path.is_file():
        raise ValueError('清晰化结果文件已不存在')
    return path


def _asset_for(manifest: dict[str, Any], source: Path) -> dict[str, Any] | None:
    from . import app as backend
    resolved = source.resolve()
    for asset in manifest.get('assets') or []:
        if not isinstance(asset, dict):
            continue
        value = (asset.get('sources') or {}).get('A') or asset.get('image_path')
        if not value:
            continue
        try:
            if backend.resolve_registered_path(str(value)).resolve() == resolved:
                return asset
        except (OSError, ValueError):
            continue
    return None


def _deletable(path: Path) -> bool:
    """原文件只允许在作品工作区/ComfyUI 输入输出里删，归档区永不删。"""
    from . import app as backend
    roots = [Path(backend.DIRECTOR_WORKSPACES_ROOT).resolve(), Path(backend.INPUT_ROOT).resolve(),
             Path(backend.OUTPUT_ROOT).resolve()]
    return any(path.resolve().is_relative_to(root) for root in roots)


def archive(project_id: str, task: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    from . import app as backend
    if task.get('status') != 'succeeded':
        raise ValueError('只有已完成的清晰化任务才能归档')
    payload = task.get('payload') or {}
    recipe = payload.get('enhancement') or {}
    if not recipe.get('source'):
        raise ValueError('任务缺少清晰化来源登记')
    output = task_output(task)
    source = backend.resolve_registered_path(str(recipe['source']))
    manifest = backend.require_project_manifest(project_id)
    asset = _asset_for(manifest, source)
    title = str((asset or {}).get('title') or source.stem)
    stem = _safe(f"{title}{TAG}")
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f'{time.strftime("%Y-%m-%d")}_{stem}{output.suffix.lower()}'
    counter = 2
    while destination.exists() and destination.stat().st_size != output.stat().st_size:
        destination = folder / f'{time.strftime("%Y-%m-%d")}_{stem}-{counter}{output.suffix.lower()}'
        counter += 1
    if not destination.exists():
        shutil.copy2(output, destination)

    overwritten = False
    deleted: str | None = None
    if overwrite:
        if asset is None:
            # 第二次点覆盖时，原件已经被删、素材登记已指向清晰化文件：按幂等处理。
            if destination.exists() and not source.exists():
                return {'path': _relative(destination), 'name': destination.name,
                        'size': destination.stat().st_size, 'overwritten': True,
                        'title': f"{title}{TAG}", 'asset_id': None, 'deleted_original': None}
            raise ValueError('找不到这份原素材的作品登记，不能覆盖；请先登记素材再试')
        current = str((asset.get('sources') or {}).get('A') or '')
        if current.startswith('素材库/清晰化/'):
            overwritten = True  # 已经覆盖过，保持幂等。
        else:
            original = source.resolve()
            asset['title'] = f"{asset.get('title') or source.stem}{TAG}"
            asset['sources'] = {**(asset.get('sources') or {}), 'A': _relative(destination)}
            if 'image_path' in asset:
                asset['image_path'] = _relative(destination)
            asset['enhanced_from'] = current
            asset['enhanced_at'] = time.time()
            manifest['assets'] = [item for item in (manifest.get('assets') or []) if isinstance(item, dict)]
            backend.save_project_manifest(manifest)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
            overwritten = True
            if original.is_file() and original != destination.resolve() and _deletable(original):
                original.unlink(missing_ok=True)
                deleted = str(original)
    return {'path': _relative(destination), 'name': destination.name,
            'size': destination.stat().st_size, 'overwritten': overwritten,
            'title': f"{title}{TAG}", 'asset_id': (asset or {}).get('id'),
            'deleted_original': deleted}
