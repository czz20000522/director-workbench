"""Two-phase local project deletion for the single-user workbench.

The private multi-user flow delegates to the external safe-delete skill; this
machine has no account scope and no such skill installed, so the same
preview-then-apply contract is implemented here against the workbench's own
roots.  A plan freezes the exact workspace paths and a file snapshot; apply
refuses when the snapshot no longer matches, so a delete can never silently
grow past what the operator previewed.  ComfyUI outputs and model files are
deliberately outside the delete scope and are reported as preserved.
"""
from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import library_archive
from ..schemas import ProjectDeletionPlanRequest, ProjectDeletionRequest

ROUTER = APIRouter()

PLAN_TTL_SECONDS = 20 * 60
PRESERVED_NOTE = ('模型文件、系统目录与其它作品的产物不会被删除；本作的工作区、任务记录、冻结快照、'
                  '以及登记在 ComfyUI 输出目录里的本作产物会一起清除，除非你在预览里勾选归档。')


def _plan_dir() -> Path:
    # Read at call time: RUNTIME is monkeypatched per test and per private layout.
    return backend.RUNTIME / 'deletion-plans'


def _workspace_root() -> Path:
    return backend.DIRECTOR_WORKSPACES_ROOT.resolve()


def _workspace_of(manifest: dict[str, Any]) -> Path:
    value = str(manifest.get('workspace_root') or '')
    if not value:
        raise HTTPException(422, '作品缺少工作区登记，不能删除')
    path = backend.resolve_registered_path(value).resolve()
    root = backend._workspace_root()
    if not path.is_relative_to(root) or path == root:
        raise HTTPException(422, f'作品工作区不在允许删除的目录内: {path}')
    return path


def _snapshot(workspace: Path) -> dict[str, Any]:
    # Sorted so the preview is stable and the drift comparison at apply time
    # cannot be fooled by filesystem enumeration order.
    files = sorted(item for item in workspace.rglob('*') if item.is_file())
    return {
        'file_count': len(files),
        'size_bytes': sum(item.stat().st_size for item in files),
        'sample': [item.relative_to(workspace).as_posix() for item in files[:10]],
    }


def _entries(project_ids: list[str]) -> list[dict[str, Any]]:
    entries = []
    for project_id in dict.fromkeys(project_ids):
        try:
            manifest = backend.load_project_manifest(project_id)
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc
        backend.ensure_no_release_blocking_tasks('删除作品', project_id=project_id)
        workspace = backend._workspace_of(manifest)
        rows = library_archive.task_rows(project_id=project_id)
        entries.append({
            'project_id': project_id,
            'title': manifest.get('title', project_id),
            'series': manifest.get('series', ''),
            'workspace': str(workspace),
            'workspace_exists': workspace.is_dir(),
            'manifest': str((backend.PROJECTS_ROOT / f'{project_id}.json')),
            'snapshot': backend._snapshot(workspace),
            'archivable': library_archive.candidates(project_id),
            'outputs': library_archive.owned_outputs(rows),
        })
    return entries


@ROUTER.get('/api/projects/{project_id}/dossier')
def project_dossier(project_id: str) -> dict[str, Any]:
    """项目档案：工作区在哪、多大、有哪些文件，以及可归档的候选。只读。"""
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace = backend._workspace_of(manifest)
    files = []
    if workspace.is_dir():
        for item in sorted(workspace.rglob('*'), key=lambda path: path.stat().st_mtime, reverse=True):
            if item.is_file():
                files.append({'path': item.relative_to(workspace).as_posix(), 'size': item.stat().st_size,
                              'modified': item.stat().st_mtime})
    try:
        archivable = library_archive.candidates(project_id)
    except (OSError, ValueError):
        archivable = []
    return {'project_id': project_id, 'title': manifest.get('title', project_id),
            'series': manifest.get('series', ''), 'workspace': str(workspace),
            'workspace_exists': workspace.is_dir(), 'file_count': len(files),
            'size_bytes': sum(item['size'] for item in files), 'files': files[:40],
            'archivable': archivable}


@ROUTER.post('/api/projects/deletion-plan')
def deletion_plan(request: ProjectDeletionPlanRequest) -> dict[str, Any]:
    entries = backend._entries(request.project_ids)
    plan_id = secrets.token_hex(12)
    plan_dir = backend._plan_dir()
    plan_dir.mkdir(parents=True, exist_ok=True)
    created = time.time()
    backend.write_json_atomic(plan_dir / f'{plan_id}.json',
                              {'plan_id': plan_id, 'created_at': created, 'entries': entries})
    return {'plan_id': plan_id, 'expires_at': created + PLAN_TTL_SECONDS,
            'entries': entries, 'preserved': PRESERVED_NOTE}


def _load_plan(plan_id: str) -> dict[str, Any]:
    if not plan_id.isalnum() or len(plan_id) != 24:
        raise HTTPException(422, '删除计划编号无效')
    path = backend._plan_dir() / f'{plan_id}.json'
    if not path.is_file():
        raise HTTPException(404, '删除计划不存在或已过期，请重新预览')
    document = json.loads(path.read_text(encoding='utf-8'))
    if time.time() - float(document.get('created_at', 0)) > PLAN_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise HTTPException(409, '删除计划已过期，请重新预览')
    return document


@ROUTER.post('/api/projects/deletion')
def deletion_apply(request: ProjectDeletionRequest) -> dict[str, Any]:
    if not request.confirm:
        raise HTTPException(422, {'code': 'confirmation_required',
                                  'message': '删除需要显式确认；不会静默执行。'})
    document = backend._load_plan(request.plan_id)
    planned = {str(item['project_id']): item for item in document.get('entries', [])}
    requested = list(dict.fromkeys(request.project_ids))
    if set(planned) != set(requested):
        raise HTTPException(409, '删除范围与预览时不一致，请重新预览')
    for project_id in requested:
        backend.ensure_no_release_blocking_tasks('删除作品', project_id=project_id)
        current = backend._snapshot(Path(planned[project_id]['workspace']))
        if current != planned[project_id]['snapshot']:
            raise HTTPException(409, f'{project_id} 的工作区内容与预览时不一致，请重新预览')

    deleted: list[str] = []
    archived: list[dict[str, Any]] = []
    removed_outputs: list[str] = []
    for project_id in requested:
        entry = planned[project_id]
        archivable = {str(item.get('asset_id')): item for item in entry.get('archivable') or []}
        chosen = [archivable[key] for key in dict.fromkeys(request.archive_asset_ids) if key in archivable]
        if chosen:
            archived.extend(library_archive.archive(chosen))
        kept_sources = {str(item.get('source')) for item in chosen}
        removed_outputs.extend(library_archive.delete_files(
            [path for path in entry.get('outputs') or [] if path not in kept_sources]))
        workspace = Path(entry['workspace'])
        snapshots = backend._task_snapshots(project_id)
        backend._purge_task_rows(project_id)
        for path in snapshots:
            path.unlink(missing_ok=True)
        # A workspace that was already gone at preview time stays gone: the
        # preview reported it, so skipping the rmtree is not a silent fallback.
        if workspace.is_dir():
            shutil.rmtree(workspace)
            backend._prune_series_directory(workspace.parent)
        Path(entry['manifest']).unlink(missing_ok=True)
        deleted.append(project_id)
    backend._retire_catalog_entries(deleted)
    backend._release_active_project(deleted)
    (backend._plan_dir() / f'{request.plan_id}.json').unlink(missing_ok=True)
    return {'deleted': deleted, 'archived': archived, 'removed_outputs': removed_outputs,
            'preserved': PRESERVED_NOTE}


def _prune_series_directory(series_dir: Path) -> None:
    """Drop the series folder once its last project workspace is gone."""
    root = backend._workspace_root()
    if series_dir == root or not series_dir.is_relative_to(root):
        return
    if series_dir.is_dir() and not any(series_dir.iterdir()):
        series_dir.rmdir()


def _task_snapshots(project_id: str) -> list[Path]:
    root = backend.SNAPSHOT_ROOT.resolve()
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(
            "SELECT json_extract(payload, '$.workflow') FROM tasks WHERE project_id = ?",
            (project_id,)).fetchall()
    paths = []
    for row in rows:
        raw = row[0]
        if not raw:
            continue
        candidate = Path(str(raw)).resolve()
        if candidate.is_relative_to(root):
            paths.append(candidate)
    return paths


def _purge_task_rows(project_id: str) -> None:
    with backend.DB_LOCK, backend.db() as connection:
        for table in ('tasks', 'task_idempotency', 'project_records'):
            connection.execute(f'DELETE FROM {table} WHERE project_id = ?', (project_id,))
        connection.commit()


def _retire_catalog_entries(project_ids: list[str]) -> None:
    dropped = set(project_ids)
    with backend.PROJECT_CATALOG_LOCK:
        catalog = backend.load_project_catalog()
        entries = [item for item in catalog.get('projects', [])
                   if isinstance(item, dict) and str(item.get('id')) not in dropped]
        catalog['projects'] = entries
        if str(catalog.get('default_project_id', '')) in dropped:
            catalog['default_project_id'] = str(entries[0]['id']) if entries else ''
        backend.write_json_atomic(backend.PROJECT_CATALOG_PATH, catalog)


def _release_active_project(project_ids: list[str]) -> None:
    """Re-point the in-memory selection once the active project is gone.

    Read against the repaired catalog, so this lands on the next remaining
    project or on the same unselected placeholder a fresh boot would use.
    """
    if backend.CURRENT_PROJECT_ID not in set(project_ids):
        return
    backend.apply_project_manifest(backend.initial_project_manifest())
