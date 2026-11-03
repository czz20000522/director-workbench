"""单步任务：不属于任何作品的快速生成，以及跨作品的批量整理。

用户要的是「我就想试一下，生成点什么」——没有作品主题，也不该在项目列表里留下
痕迹。服务端用一个隐藏容器 ``quick-tasks`` 承载任务行、冻结快照和产物归属，因为
后端到处假设任务有 project_id；这个容器从不出现在 /api/projects 里，用户看到的
只是一条条以提示词开头命名的单步任务。

批量删除沿用作品删除的同一份两段式合同：预览冻结范围，apply 必须显式确认，
状态或范围与预览不一致就拒绝，ComfyUI 输出目录里的产物始终不动。
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import app as backend
from .. import library_archive
from .. import task_scheduler
from ..schemas import TaskBatchDeleteRequest, TaskBatchPlanRequest


ROUTER = APIRouter()

QUICK_PROJECT_ID = 'quick-tasks'
QUICK_SERIES = '单步任务'
QUICK_TITLE = '单步任务'
QUICK_WORKSPACE_DIRECTORY = '_单步任务'
TASK_PLAN_TTL_SECONDS = 20 * 60
TOOL_LABELS = {'keyframe': '图片', 'workflow': '视频', 'enhancement': '清晰化', 'media_operation': '媒体处理', 'music': '配乐',
               'audio_edit': '音频编辑', 'audio_master': '全片母版', 'speech': '配音',
               'anchor_dubbing': '锚点配音', 'audio_swap': '换音轨', 'vlm_observation': '画面观察',
               'reference_decomposition': '参考拆解', 'reference_semantic': '参考分析'}


def task_plan_directory() -> Path:
    return backend.RUNTIME / 'task-deletion-plans'


def ensure_quick_project() -> str:
    """Create the hidden quick container once, then hand back its id."""
    try:
        backend.load_project_manifest(QUICK_PROJECT_ID)
        return QUICK_PROJECT_ID
    except RuntimeError:
        pass
    workspace = (backend.DIRECTOR_WORKSPACES_ROOT / QUICK_WORKSPACE_DIRECTORY).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    for folder in ('plans', 'assets/image', 'assets/audio', 'assets/video', 'workflows/ui', 'workflows/api',
                   'records/prompts', 'records/tasks', 'records/reviews', 'keyframe-tasks'):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    manifest = backend.blank_project_manifest(QUICK_PROJECT_ID, QUICK_SERIES, QUICK_TITLE, workspace)
    backend.persist_project_manifest(manifest, make_default=False)
    return QUICK_PROJECT_ID


def task_tool(payload: dict[str, Any]) -> str:
    """配音 that starts from a written timbre description is the 音色设计 tool, not a dialogue take."""
    if is_enhancement(payload):
        return '清晰化'
    if payload.get('kind') == 'speech' and (payload.get('request') or {}).get('mode') == 'design':
        return '音色设计'
    return TOOL_LABELS.get(str(payload.get('kind')), '任务')


def is_enhancement(payload: dict[str, Any]) -> bool:
    """清晰化 also runs a ComfyUI graph, so its payload kind says 'workflow'."""
    return bool(payload.get('enhancement')) or payload.get('pipeline_stage_id') == 'media-enhancement'


def _portable(value: Any) -> str | None:
    """One path form for every reuse value, matching what the panels already accept."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return backend.root_relative_path(backend.resolve_registered_path(value))
    except (ValueError, OSError, HTTPException):
        return None


def _aspect(width: Any, height: Any) -> str | None:
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    return '1:1' if width == height else ('16:9' if width > height else '9:16')


MUSIC_FORM_FIELDS = ('tags', 'lyrics', 'duration_seconds', 'bpm', 'keyscale', 'timesignature', 'language',
                     'steps', 'cfg', 'shift', 'quality', 'generate_audio_codes', 'encode_cfg_scale',
                     'temperature', 'top_p', 'top_k', 'min_p')


def quick_task_reuse(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The editable inputs of a finished single-step task, in the tool form's own field names.

    首页「编辑重生成」把这些值带回对应工具，由你改完再显式提交——工作台不会自动重跑。
    带不全的工具就不给这个入口：表单看着像上次、其实不是，比没有入口更糟。
    """
    if is_enhancement(payload):
        source = _portable((payload.get('enhancement') or {}).get('source'))
        return {'tool': 'enhance', 'values': {'source': source}} if source else None
    kind = str(payload.get('kind') or '')
    if kind == 'keyframe':
        request = payload.get('request') or {}
        prompt = str(request.get('prompt') or '').strip()
        if not prompt or request.get('mode') != 'text':
            return None  # 参考图模式的起点是一张图，不在这里替你猜
        size = payload.get('output_size') or {}
        values: dict[str, Any] = {'prompt': prompt,
                                  'quality': str(request.get('output_resolution') or size.get('resolution') or 'native')}
        if type(request.get('seed')) is int:
            values['seed'] = request['seed']
        if type(request.get('steps')) is int:
            values['steps'] = request['steps']
        aspect = _aspect(request.get('width'), request.get('height'))
        if aspect:
            values['aspect'] = aspect
        return {'tool': 'image', 'values': values}
    if kind == 'workflow':
        snapshot = payload.get('segment_snapshot') or {}
        generation = snapshot.get('generation') or {}
        prompt = str(payload.get('prompt_override') or payload.get('prompt') or snapshot.get('prompt') or '').strip()
        if not prompt:
            return None
        values = {'prompt': prompt}
        frame = _portable(payload.get('first_frame') or (snapshot.get('keyframes') or {}).get('first'))
        if frame:
            values['frame'] = frame
        duration = snapshot.get('duration_seconds')
        if type(duration) in (int, float) and duration > 0:
            values['duration'] = int(duration) if float(duration).is_integer() else float(duration)
        if generation.get('aspect_ratio') in ('1:1', '16:9', '9:16'):
            values['aspect'] = generation['aspect_ratio']
        if generation.get('output_resolution'):
            values['quality'] = str(generation['output_resolution'])
        return {'tool': 'image-video' if frame else 'text-video', 'values': values}
    if kind == 'speech':
        request = payload.get('request') or {}
        if request.get('mode') != 'design':
            return None  # 克隆模式的起点是一段参考音频，得你自己重新选
        description = str(request.get('voice_description') or '').strip()
        if not description:
            return None
        values = {'timbre': description, 'text': str(request.get('text') or '')}
        seed = (request.get('parameters') or {}).get('seed')
        if type(seed) is int:
            values['seed'] = seed
        return {'tool': 'timbre', 'values': values}
    if kind == 'music':
        frozen = payload.get('music') or {}
        if not str(frozen.get('tags') or '').strip():
            return None
        values = {'music': {key: frozen[key] for key in MUSIC_FORM_FIELDS if key in frozen}}
        if frozen.get('format'):
            values['music']['output_format'] = frozen['format']  # 表单里叫 output_format
        if type(frozen.get('seed')) is int:
            values['seed'] = frozen['seed']
        return {'tool': 'music', 'values': values}
    return None


def quick_prompt_excerpt(task: dict[str, Any], length: int = 46) -> str:
    payload = task.get('payload') or {}
    prompt = backend._prompt_of(task)
    if not prompt and is_enhancement(payload):
        prompt = Path(str((payload.get('enhancement') or {}).get('source') or '')).stem
    collapsed = ' '.join(prompt.split())
    if not collapsed:
        return backend.task_tool(payload)
    return collapsed if len(collapsed) <= length else collapsed[:length].rstrip() + '…'


def quick_task_card(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get('payload') or {}
    result = task.get('result') or {}
    enhance = is_enhancement(payload)
    outputs = (result or {}).get('published_outputs') or [f"output/{item}" for item in (result or {}).get('outputs') or []]
    return {
        'id': task['id'], 'kind': payload.get('kind'),
        'tool': backend.task_tool(payload),
        # 视频任务再分文生/图生：有首帧就是图生视频，没有就是文生视频。
        'video_mode': (('image-video' if payload.get('first_frame') else 'text-video')
                       if payload.get('kind') == 'workflow' and not enhance else None),
        'title': backend.quick_prompt_excerpt(task), 'status': task['status'],
        'created_at': task.get('created_at'), 'updated_at': task.get('updated_at'),
        'output': ((result or {}).get('output') or (result or {}).get('video_path')
                   or (outputs[0] if enhance and len(outputs) == 1 else None)),
        'error': task.get('error'), 'revision': payload.get('revision', 0),
        'retry_of': payload.get('retry_of'), 'queue': task.get('queue') or {},
        'progress': backend.comfy_progress.visible_progress(task),
        'reuse': backend.quick_task_reuse(payload),
        'deletable': backend._delete_blocker(task) is None,
        'delete_blocker': backend._delete_blocker(task),
    }


@ROUTER.get('/api/quick-workspace')
def quick_workspace() -> dict[str, Any]:
    """The container id single-step tools submit against; created on first use."""
    return {'project_id': backend.ensure_quick_project()}


@ROUTER.get('/api/quick-tasks')
def quick_task_list(limit: int = Query(default=60, ge=1, le=200)) -> dict[str, Any]:
    """List single-step tasks as their own thing, titled by the prompt that made them."""
    backend.ensure_quick_project()
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT ?',
                                  (QUICK_PROJECT_ID, limit)).fetchall()
    tasks = [backend.task_row(row) for row in rows]
    for task in tasks:
        task['queue'] = backend.TASK_SCHEDULER.queue_info(task['id'])
    return {'project_id': QUICK_PROJECT_ID,
            'tasks': [backend.quick_task_card(task) for task in tasks]}


def _batch_rows(task_ids: list[str]) -> list[dict[str, Any]]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks ORDER BY created_at DESC').fetchall()
    by_id = {str(row['id']): backend.task_row(row) for row in rows}
    selected: list[dict[str, Any]] = []
    for task_id in dict.fromkeys(task_ids):
        task = by_id.get(task_id)
        if not task:
            raise HTTPException(404, f'任务不存在：{task_id}')
        principal = backend.current_principal.get()
        if backend.PRIVATE_WORKSPACES is not None and principal is not None:
            backend.PRIVATE_WORKSPACES.verify_task(task)
        selected.append(task)
    return selected


@ROUTER.post('/api/tasks/deletion-plan')
def tasks_deletion_plan(request: TaskBatchPlanRequest) -> dict[str, Any]:
    """Freeze which tasks would go, and what their removal touches."""
    entries = []
    for task in backend._batch_rows(request.task_ids):
        blocker = backend._delete_blocker(task)
        if blocker:
            raise HTTPException(409, f'{backend.quick_prompt_excerpt(task)}：{blocker}')
        files = backend._snapshot_files(task, str(task.get('project_id') or ''))
        entries.append({'task_id': task['id'], 'project_id': task.get('project_id'), 'status': task['status'],
                        'title': backend.quick_prompt_excerpt(task), 'asset_id': task.get('asset_id'),
                        'snapshots': [str(path) for path in files]})
    plan_id = secrets.token_hex(12)
    directory = backend.task_plan_directory()
    directory.mkdir(parents=True, exist_ok=True)
    created = time.time()
    rows = backend._batch_rows([entry['task_id'] for entry in entries])
    archivable: list[dict[str, Any]] = []
    for project_id in dict.fromkeys(str(row.get('project_id') or '') for row in rows):
        ids = {row['id'] for row in rows if str(row.get('project_id') or '') == project_id}
        archivable.extend(library_archive.candidates(project_id, ids))
    outputs = library_archive.owned_outputs(rows)
    backend.write_json_atomic(directory / f'{plan_id}.json',
                              {'plan_id': plan_id, 'created_at': created, 'entries': entries,
                               'archivable': archivable, 'outputs': outputs})
    return {'plan_id': plan_id, 'expires_at': created + TASK_PLAN_TTL_SECONDS, 'entries': entries,
            'archivable': archivable, 'outputs': outputs,
            'preserved': '模型文件与其它任务的产物不会被删除；这些任务在 ComfyUI 输出目录里的产物会一起清除，除非你勾选归档。'}


def _load_task_plan(plan_id: str) -> dict[str, Any]:
    if not plan_id.isalnum() or len(plan_id) != 24:
        raise HTTPException(422, '删除计划编号无效')
    path = backend.task_plan_directory() / f'{plan_id}.json'
    if not path.is_file():
        raise HTTPException(404, '删除计划不存在或已过期，请重新预览')
    document = json.loads(path.read_text(encoding='utf-8'))
    if time.time() - float(document.get('created_at', 0)) > TASK_PLAN_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise HTTPException(409, '删除计划已过期，请重新预览')
    return document


@ROUTER.post('/api/tasks/deletion')
def tasks_deletion_apply(request: TaskBatchDeleteRequest) -> dict[str, Any]:
    """Delete exactly the tasks the preview listed, refusing any drift."""
    if not request.confirm:
        raise HTTPException(422, {'code': 'confirmation_required',
                                  'message': '删除任务需要显式确认；不会静默执行。'})
    document = backend._load_task_plan(request.plan_id)
    planned = {str(entry['task_id']): entry for entry in document.get('entries', [])}
    requested = list(dict.fromkeys(request.task_ids))
    if set(planned) != set(requested):
        raise HTTPException(409, '删除范围与预览时不一致，请重新预览')
    current = {task['id']: task for task in backend._batch_rows(requested)}
    for task_id, entry in planned.items():
        task = current[task_id]
        if task['status'] != entry['status']:
            raise HTTPException(409, f'{entry["title"]} 的状态已变为 {task["status"]}，请重新预览')
        blocker = backend._delete_blocker(task)
        if blocker:
            raise HTTPException(409, f'{entry["title"]}：{blocker}')

    archivable = {str(item.get('asset_id')): item for item in document.get('archivable') or []}
    chosen = [archivable[key] for key in dict.fromkeys(request.archive_asset_ids) if key in archivable]
    archived = library_archive.archive(chosen) if chosen else []
    kept_sources = {str(item.get('source')) for item in chosen}
    removed_outputs = library_archive.delete_files(
        [path for path in document.get('outputs') or [] if path not in kept_sources])
    purged_assets: dict[str, set[str]] = {}
    for item in chosen:
        purged_assets.setdefault(str(item.get('project_id') or ''), set()).add(str(item.get('asset_id') or ''))
    deleted: list[str] = []
    removed: list[str] = []
    for task_id in requested:
        task = current[task_id]
        entry = planned[task_id]
        if task['status'] == 'scheduler_waiting':
            backend.TASK_SCHEDULER.cancel_pending(task_id)
        project_id = str(task.get('project_id') or '')
        stale = backend._unreferenced([Path(path) for path in entry['snapshots']], set(requested))
        with backend.DB_LOCK, backend.db() as connection:
            connection.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            connection.execute('DELETE FROM task_idempotency WHERE task_id = ?', (task_id,))
            connection.execute('DELETE FROM resource_samples WHERE task_id = ?', (task_id,))
            connection.commit()
        for path in stale:
            if path.is_file():
                path.unlink(missing_ok=True)
                removed.append(str(path))
            # 任务自己的冻结目录（keyframe-tasks/<task_id>/）删空后一并移除，
            # 否则会在工作区留下一个空目录。共享模板目录名不是任务 id，不受影响。
            holder = path.parent
            if holder.name == task_id and holder.is_dir() and not any(holder.iterdir()):
                holder.rmdir()
        deleted.append(task_id)
        purged_assets.setdefault(project_id, set()).update({str(entry.get('asset_id') or '')})
    for project_id, ids in purged_assets.items():
        if project_id:
            library_archive.prune_assets(project_id, {item for item in ids if item})
    backend.TASK_SCHEDULER.wake()
    backend.task_plan_directory().joinpath(f'{request.plan_id}.json').unlink(missing_ok=True)
    return {'deleted': deleted, 'archived': archived, 'removed_snapshots': removed,
            'removed_outputs': removed_outputs,
            'preserved': '模型文件与其它任务的产物未被删除。'}
