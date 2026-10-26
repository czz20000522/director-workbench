"""任务队列的人工整理入口：跨作品列表、删除、派发前的显式修改与改参数重跑。

单用户模式此前既没有跨作品的任务行入口（``/api/queue`` 只给计数），也没有任何
删除或修改任务的路由。这里补齐三件事，并守住既有的冻结不变量：提示词在冻结时
已经写进不可变的 api 快照，所以任务一旦派发就不再原地改 payload；改参数要么发生
在派发之前，要么以「另起一条带 ``retry_of`` 的新任务」的形式留下溯源。ComfyUI
输出目录里的图片与成片始终不动。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from .. import app as backend
from .. import keyframes, task_scheduler
from ..schemas import TaskDeleteRequest, TaskEditRequest


ROUTER = APIRouter()

PRESERVED_NOTE = 'ComfyUI 输出目录中的图片与成片不会被删除；只删除任务记录、幂等键、资源采样与不再被引用的冻结工作流快照。'
PENDING = 'scheduler_waiting'
BATCH_PENDING = 'batch_waiting'
EDITABLE_KIND = 'keyframe'


def _prompt_of(task: dict[str, Any]) -> str:
    payload = task.get('payload') or {}
    snapshot = payload.get('execution_snapshot') or {}
    request = payload.get('request') or {}
    # Speech tasks carry no prompt field; the line itself (or the timbre being
    # designed) is the only thing that names the task for the user.
    for value in (snapshot.get('prompt'), request.get('prompt'),
                  (payload.get('pipeline_values') or {}).get('prompt'), payload.get('prompt'),
                  request.get('voice_description'), request.get('text')):
        if value:
            return str(value)
    return ''


def _delete_blocker(task: dict[str, Any]) -> str | None:
    status = str(task.get('status'))
    if status in task_scheduler.TERMINAL:
        return None
    if status == PENDING and not task.get('prompt_id'):
        return None
    if status == BATCH_PENDING:
        return '批次成员请先停止整个批次再删除，避免打断批次顺序'
    return f'任务当前状态为 {status}，请先停止并等待核对完成后再删除'


def _edit_blocker(task: dict[str, Any]) -> str | None:
    if str(task.get('status')) != PENDING or task.get('prompt_id'):
        return '任务已派发，提示词已经烘进冻结工作流，不能再原地修改；请改用「改参数重跑」另起一条任务'
    if (task.get('payload') or {}).get('kind') != EDITABLE_KIND:
        return '只有文生图与关键帧任务可以在队列里改参数；视频镜头的提示词来自分镜计划，请在镜头步骤修改后重新提交'
    return None


def _snapshot_files(task: dict[str, Any], project_id: str) -> list[Path]:
    root = backend.task_snapshot_root(project_id).resolve()
    payload = task.get('payload') or {}
    snapshot = payload.get('execution_snapshot') or {}
    files: list[Path] = []
    for value in (payload.get('workflow'), payload.get('source_workflow'), snapshot.get('api_graph')):
        if not value:
            continue
        path = Path(str(value)).resolve()
        if path.is_relative_to(root) and path.is_file() and path not in files:
            files.append(path)
    return files


def _unreferenced(paths: list[Path], keep_task_ids: set[str]) -> list[Path]:
    """Drop snapshots that no surviving task row still points at."""
    if not paths:
        return []
    kept = sorted(keep_task_ids)
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(f"SELECT payload FROM tasks WHERE id NOT IN ({','.join('?' for _ in kept)})", kept).fetchall()
    referenced: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row[0] or '{}')
        except json.JSONDecodeError:
            continue
        snapshot = payload.get('execution_snapshot') or {}
        for value in (payload.get('workflow'), payload.get('source_workflow'), snapshot.get('api_graph')):
            if value:
                referenced.add(str(Path(str(value)).resolve()))
    return [path for path in paths if str(path) not in referenced]


def _task_project_titles(project_ids: set[str]) -> dict[str, str]:
    """Resolve a label for every project a listed task belongs to.

    The catalog is tried first because it is one read, but a task can outlive a
    catalog entry, so the manifest is the fallback and the bare id the last resort
    — a deleted work must still show up in the queue rather than hide its tasks.
    """
    catalog = {str(entry.get('id')): str(entry.get('title') or '') for entry in backend.project_entries()}
    titles: dict[str, str] = {}
    for project_id in project_ids:
        title = catalog.get(project_id) or ''
        if not title:
            try:
                title = str(backend.load_project_manifest(project_id).get('title') or '')
            except (RuntimeError, OSError, ValueError):
                title = ''
        titles[project_id] = title or project_id
    return titles


@ROUTER.get('/api/queue/tasks')
def queue_tasks(limit: int = Query(default=80, ge=1, le=300), project_id: str | None = None,
                state: Literal['all', 'active', 'terminal'] = 'all') -> dict[str, Any]:
    """List task rows across works, with what a human can now do to each one."""
    with backend.DB_LOCK, backend.db() as connection:
        if project_id:
            rows = connection.execute('SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT ?',
                                      (project_id, limit)).fetchall()
        else:
            rows = connection.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    tasks = [backend.task_row(row) for row in rows]
    principal = backend.current_principal.get()
    if principal is not None and not principal.administrator:
        tasks = [item for item in tasks if (item['payload'] or {}).get('owner_user') == principal.identity.username]
    if state != 'all':
        tasks = [item for item in tasks if (item['status'] in task_scheduler.TERMINAL) == (state == 'terminal')]
    titles = backend._task_project_titles({str(item.get('project_id') or '') for item in tasks})
    queue = backend.TASK_SCHEDULER.queue_info_many([item['id'] for item in tasks])
    items = []
    for task in tasks:
        payload = task['payload'] or {}
        request = payload.get('request') or {}
        items.append({
            'id': task['id'], 'project_id': task.get('project_id'),
            'project_title': titles.get(str(task.get('project_id')), str(task.get('project_id') or '已删除作品')),
            'asset_id': task.get('asset_id'), 'kind': payload.get('kind'), 'status': task['status'],
            'prompt_id': task.get('prompt_id'), 'batch_id': payload.get('batch_id') or task.get('batch_id'),
            'sequence': task.get('sequence'), 'stop_requested': task.get('stop_requested'),
            'created_at': task.get('created_at'), 'updated_at': task.get('updated_at'),
            'error': task.get('error'), 'prompt': backend._prompt_of(task), 'revision': payload.get('revision', 0),
            'retry_of': payload.get('retry_of'), 'seed': request.get('seed', (payload.get('pipeline_values') or {}).get('seed')),
            'width': request.get('width'), 'height': request.get('height'),
            'result': task.get('result'), 'queue': queue.get(task['id'], {}),
            'progress': backend.comfy_progress.visible_progress(task),
            'deletable': backend._delete_blocker(task) is None, 'editable': backend._edit_blocker(task) is None,
            'delete_blocker': backend._delete_blocker(task), 'edit_blocker': backend._edit_blocker(task),
        })
    summary = backend.TASK_SCHEDULER.summary()
    return {'gpu_slots': summary.get('gpu_slots', 1), 'cpu_slots': summary.get('cpu_slots'),
            'status_counts': summary.get('status_counts', {}), 'tasks': items}


def _rebuild_keyframe(task: dict[str, Any], manifest: dict[str, Any], edits: dict[str, Any], *,
                      task_id: str, prefix: str) -> dict[str, Any]:
    """Re-run the same builder the task came from, with the edited values applied."""
    payload = dict(task['payload'])
    request = dict(payload.get('request') or {})
    prompt = str(edits.get('prompt') or request.get('prompt') or '')
    if not prompt:
        raise HTTPException(409, '任务缺少原始提示词，无法重建')
    profile = str(request.get('creative_profile') or 'general')
    backend.validate_creative_prompt(prompt)
    width = int(edits.get('width') or request.get('width') or 768)
    height = int(edits.get('height') or request.get('height') or 1344)
    if width % 8 or height % 8:
        raise HTTPException(422, '图像宽高须为 8 的倍数')
    seed = int(edits['seed']) if edits.get('seed') is not None else int(request.get('seed') or 0)
    workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
    rebuilt = keyframes.prepare_text(task_id, prompt, workspace / 'keyframe-tasks', prefix, seed, width, height, profile)
    for key in ('project_id', 'owner_user'):
        if payload.get(key):
            rebuilt[key] = payload[key]
    return rebuilt


@ROUTER.patch('/api/projects/{project_id}/tasks/{task_id}')
@backend.gpu_admission
def edit_task(project_id: str, task_id: str, request: TaskEditRequest) -> dict[str, Any]:
    """Rewrite a task that has not been dispatched yet, and re-freeze its workflow."""
    manifest = backend.require_project_manifest(project_id)
    task = backend.get_task(task_id)
    if not task or task.get('project_id') != project_id:
        raise HTTPException(404, '该作品中不存在此任务')
    blocker = backend._edit_blocker(task)
    if blocker:
        raise HTTPException(409, blocker)
    edits = {key: value for key, value in request.model_dump().items() if value is not None}
    if not edits:
        raise HTTPException(422, '没有需要修改的字段')
    revision = int((task['payload'] or {}).get('revision') or 0) + 1
    prefix = str((task['payload'].get('request') or {}).get('output_prefix') or f'导演工作台工作目录/{project_id}/关键帧/{task_id}')
    payload = backend._rebuild_keyframe(task, manifest, edits, task_id=f'{task_id}-r{revision}', prefix=prefix)
    payload['asset_id'] = task['asset_id']
    receipt = (task['payload'] or {}).get('submission_receipt')
    if isinstance(receipt, dict):
        # The key still names this task, so its recorded request has to describe the
        # parameters that will actually run; a stale copy would 409 a legitimate replay.
        receipt = dict(receipt)
        receipt['request'] = {**(receipt.get('request') or {}),
                              **{key: payload['request'][key] for key in ('prompt', 'seed', 'width', 'height', 'creative_profile') if key in payload['request']}}
    payload['submission_receipt'] = receipt
    payload['revision'] = revision
    payload['edited'] = {'at': time.time(), 'fields': sorted(edits), 'from_revision': revision - 1}
    stale = backend._unreferenced(backend._snapshot_files(task, project_id), {task_id})
    backend.set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    for path in stale:
        path.unlink(missing_ok=True)
    return backend.get_task(task_id)


@ROUTER.post('/api/projects/{project_id}/tasks/{task_id}/revise')
@backend.gpu_admission
def revise_task(project_id: str, task_id: str, request: TaskEditRequest) -> dict[str, Any]:
    """Derive a fresh task with edited parameters, keeping the original row as history."""
    manifest = backend.require_project_manifest(project_id)
    task = backend.get_task(task_id)
    if not task or task.get('project_id') != project_id:
        raise HTTPException(404, '该作品中不存在此任务')
    if (task['payload'] or {}).get('kind') != EDITABLE_KIND:
        raise HTTPException(409, '只有文生图与关键帧任务可以改参数重跑；视频镜头请在镜头步骤修改后重新提交')
    edits = {key: value for key, value in request.model_dump().items() if value is not None}
    if not edits:
        raise HTTPException(422, '没有需要修改的字段')
    backend.require_submission_capacity()
    readiness = backend.keyframe_capability(mode='text')
    if not readiness['available']:
        raise HTTPException(503, {'message': '图像生成环境尚未就绪', **readiness})
    new_id = str(uuid.uuid4())
    prefix = f'导演工作台工作目录/{project_id}/关键帧/{new_id}'
    payload = backend._rebuild_keyframe(task, manifest, edits, task_id=new_id, prefix=prefix)
    payload.update(project_id=project_id, asset_id=f'keyframe-{new_id}', retry_of=task_id, revision=0)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (new_id, payload['asset_id'], PENDING, now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(new_id)
    return backend.get_task(new_id)


@ROUTER.delete('/api/projects/{project_id}/tasks/{task_id}')
@backend.gpu_admission
def delete_task(project_id: str, task_id: str, request: TaskDeleteRequest) -> dict[str, Any]:
    """Remove one task record and the snapshots nobody else references."""
    if not request.confirm:
        raise HTTPException(422, {'code': 'confirmation_required', 'message': '删除任务需要显式确认；不会静默执行。'})
    backend.require_project_manifest(project_id)
    task = backend.get_task(task_id)
    if not task or task.get('project_id') != project_id:
        raise HTTPException(404, '该作品中不存在此任务')
    blocker = backend._delete_blocker(task)
    if blocker:
        raise HTTPException(409, blocker)
    if task['status'] == PENDING:
        backend.TASK_SCHEDULER.cancel_pending(task_id)
    files = backend._unreferenced(backend._snapshot_files(task, project_id), {task_id})
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        connection.execute('DELETE FROM task_idempotency WHERE task_id = ?', (task_id,))
        connection.execute('DELETE FROM resource_samples WHERE task_id = ?', (task_id,))
        connection.commit()
    for path in files:
        path.unlink(missing_ok=True)
    backend.TASK_SCHEDULER.wake()
    return {'deleted': task_id, 'status': task['status'], 'removed_snapshots': [str(path) for path in files],
            'preserved': PRESERVED_NOTE}
