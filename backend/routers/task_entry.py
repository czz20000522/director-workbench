"""任务入口与补齐：任务列表与详情、工作台启动入口、补齐（finish/enhancement/submission）任务。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import uuid
import av
from fastapi import APIRouter, HTTPException, Query
import importlib.util
from urllib.error import HTTPError, URLError
from .. import enhancement
from ..schemas import FinishRequest, StartRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 100", (backend.CURRENT_PROJECT_ID,)).fetchall()
    result = [backend.task_row(row) for row in rows]
    queue_values = backend.TASK_SCHEDULER.queue_info_many([task['id'] for task in result])
    for task in result:
        task['queue'] = queue_values.get(task['id'], {})
    return result


@ROUTER.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    task = backend.get_current_project_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@ROUTER.post("/api/tasks")
@backend.gpu_admission
def start(request: StartRequest) -> dict[str, Any]:
    return backend.submit_project_task(request)


@ROUTER.post("/api/projects/{project_id}/tasks")
@backend.gpu_admission
def start_project_task(project_id: str, request: StartRequest) -> dict[str, Any]:
    return backend.submit_project_task(request, backend.require_project_manifest(project_id))


def build_finish_workflow_snapshot(project_id: str, asset_id: str, source: Path, model_name: str,
                                   color_reference: str | None, color_method: str, color_strength: float) -> Path:
    """Build and persist the immutable API graph for a finish task."""
    builder = backend.validate_execution_script_path(backend.FINISH_BUILDER, allowed_roots=(backend.PROJECT, backend.CODE_ROOT), label='高清处理构建器')
    if not builder.is_file():
        raise FileNotFoundError(f'缺少高清处理工作流构建器: {builder}')
    module_spec = importlib.util.spec_from_file_location('director_finish_builder', builder)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f'无法加载高清处理工作流构建器: {builder}')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    project_label = ''.join(char if char.isalnum() or char in '-_' else '-' for char in project_id)
    asset_label = ''.join(char if char.isalnum() or char in '-_' else '-' for char in asset_id)
    prefix = f'导演工作台工作目录/{project_label}/video/{asset_label}-finish-{uuid.uuid4().hex[:10]}'
    graph = module.build_graph(str(source), prefix, model_name=model_name,
                               color_reference=color_reference, color_method=color_method,
                               color_strength=color_strength)
    destination = backend.task_snapshot_root(project_id) / f'finish-{uuid.uuid4().hex[:16]}.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return destination


@ROUTER.post('/api/projects/{project_id}/finish-tasks', status_code=202)
@backend.gpu_admission
@backend.plan_transaction
def start_finish_task(project_id: str, request: FinishRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    backend.require_submission_capacity()
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_plan_revision)
    segment = next((item for item in document.get('segments', [])
                    if isinstance(item, dict) and item.get('id') == request.asset_id), None)
    if segment is None:
        raise HTTPException(404, '分镜不存在')
    state = backend.build_project_state(project_id)
    sample_review = (state.get('reviews', {}).get(request.asset_id, {}) or {}).get('sample') or {}
    if sample_review.get('status') != 'approved':
        raise HTTPException(409, '请先采用样片，再生成 AI 高清候选')
    if int(sample_review.get('revision', -1)) != request.expected_review_revision:
        raise HTTPException(409, {'code': 'record_revision_conflict', 'message': '样片审核已更新，请重新读取后再生成高清候选', 'current_revision': sample_review.get('revision')})
    video = segment.get('video') or {}
    source_ref = sample_review.get('candidate_ref') or video.get('path')
    if not source_ref:
        raise HTTPException(409, '当前样片缺少可处理的视频文件')
    try:
        source = backend.adoption_candidate_path(str(source_ref), project_id)
    except HTTPException:
        raise
    if not source.is_file() or not backend.same_registered_media(str(video.get('path') or ''), str(source)):
        raise HTTPException(409, '当前采用样片已变化，请重新审核后再生成高清候选')
    if request.color_reference:
        try:
            color_reference_path = backend.resolve_registered_path(request.color_reference)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, f'色彩参考不可访问: {request.color_reference}') from exc
        if not color_reference_path.is_file():
            raise HTTPException(422, '色彩参考文件不存在')
        color_reference_value = str(color_reference_path)
    else:
        color_reference_value = None
    submission_request = request.model_dump(mode='json')
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if (existing.get('payload') or {}).get('submission_receipt', {}).get('request') != submission_request:
            raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '该请求标识已用于不同高清处理参数', 'task_id': existing['id']})
        return existing
    backend.ensure_asset_not_pending(project_id, request.asset_id)
    workflow = backend.build_finish_workflow_snapshot(project_id, request.asset_id, source, request.model_name,
                                               color_reference_value, request.color_method, request.color_strength)
    source_identity = {
        'path': str(video.get('path') or source_ref),
        'task_id': segment.get('current_version_task_id'),
        'prompt_id': segment.get('comfyui_task_id'),
        'review_revision': request.expected_review_revision,
    }
    payload = {
        'kind': 'workflow', 'pipeline_stage_id': 'picture-finish', 'asset_id': request.asset_id,
        'project_id': project_id, 'plan_path': str(plan_path),
        'assembly_asset_id': str(manifest.get('assembly_asset_id', 'MASTER')),
        'workflow': str(workflow), 'finish_model': request.model_name,
        'finish_source': source_identity, 'segment_snapshot': backend.segment_generation_snapshot(segment),
        'finish_options': {'color_method': request.color_method, 'color_strength': request.color_strength,
                           'color_reference': color_reference_value},
        'submission_receipt': {'key': request.idempotency_key, 'request': submission_request},
    }
    try:
        payload = backend.freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f'无法冻结高清处理快照: {exc}') from exc
    task_id = str(uuid.uuid4())
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, request.asset_id, 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id) or {'id': task_id, 'status': 'preparing'}


def enhancement_sources(project_id: str) -> list[dict[str, str]]:
    """Only registered creative media and this work's successful task outputs."""
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    paths: dict[str, dict[str, str]] = {}

    def add(value, label):
        if not isinstance(value, str) or not value:
            return
        try:
            path = backend.resolve_registered_path(value)
            if path.is_file() and path.suffix.lower() in enhancement.IMAGE_EXTENSIONS | enhancement.VIDEO_EXTENSIONS:
                # One portable path form for every entry: upload receipts and plan
                # entries already use it, and the panel selects by exact value.
                paths.setdefault(str(path).casefold(), {'value': backend.root_relative_path(path), 'label': label,
                    'kind': 'image' if path.suffix.lower() in enhancement.IMAGE_EXTENSIONS else 'video'})
        except (ValueError, OSError, HTTPException):
            pass

    for item in backend.reusable_materials(project_id)['materials']:
        add(item['path'], item['title'])
    for segment in document.get('segments', []):
        label = str(segment.get('id'))
        add((segment.get('video') or {}).get('path'), f'{label} · 当前视频')
        add(((segment.get('video') or {}).get('finish_review') or {}).get('path'), f'{label} · 高清候选')
        for key, value in (segment.get('keyframes') or {}).items():
            add(value, f'{label} · {key}')
    add((document.get('assembly') or {}).get('output'), '装配成片候选')
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id=? AND status='succeeded' ORDER BY created_at DESC", (project_id,)).fetchall()
    for row in rows:
        task = backend.task_row(row)
        if (task.get('payload') or {}).get('kind') == 'media_operation':
            continue
        result = task.get('result') or {}
        for value in result.get('published_outputs') or ([result['output']] if result.get('output') else [f'output/{v}' for v in result.get('outputs', []) if isinstance(v, str)]):
            add(value, f"{task['asset_id']} · 候选 {task['id'][:8]}")
    return list(paths.values())


@ROUTER.get('/api/projects/{project_id}/enhancements')
def project_enhancements(project_id: str, offset: int = Query(0, ge=0)) -> dict[str, Any]:
    sources = backend.enhancement_sources(project_id)
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id=? AND json_extract(payload, '$.pipeline_stage_id')='media-enhancement' ORDER BY created_at DESC, id DESC LIMIT 21 OFFSET ?", (project_id, offset)).fetchall()
    tasks = [backend.task_row(row) for row in rows[:20]]
    queue_values = backend.TASK_SCHEDULER.queue_info_many([task['id'] for task in tasks])
    for task in tasks:
        task['queue'] = queue_values.get(task['id'], {})
    return {'sources': sources, 'tasks': tasks, 'next_offset': offset + 20 if len(rows) > 20 else None}


@ROUTER.get('/api/projects/{project_id}/enhancement-capability')
def enhancement_capability(project_id: str) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    try:
        return enhancement.capability(backend.request_json('/object_info'))
    except (OSError, ValueError, HTTPError, URLError) as exc:
        raise HTTPException(503, '无法检查 ComfyUI 清晰化能力，请确认引擎在线') from exc


@ROUTER.post('/api/projects/{project_id}/enhancement-tasks', status_code=202)
@backend.gpu_admission
@backend.plan_transaction
def start_enhancement_task(project_id: str, request: enhancement.EnhancementRequest) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    submission = request.model_dump(mode='json')
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if (existing.get('payload') or {}).get('submission_receipt', {}).get('request') != submission:
            raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '请求标识已用于其他参数'})
        return existing
    backend.require_submission_capacity()
    try:
        source = backend.resolve_registered_path(request.source)
        admitted = {backend.resolve_registered_path(item['value']) for item in backend.enhancement_sources(project_id)}
        if source not in admitted:
            raise ValueError('请选择当前作品已登记的图片或视频')
        stamp = source.stat()
        facts = enhancement.inspect_source(source)
    except (ValueError, OSError, av.error.FFmpegError) as exc:
        raise HTTPException(422, f'无法处理素材: {exc}') from exc
    capability = backend.enhancement_capability(project_id)
    if not capability['kinds'][facts['kind']]['available']:
        raise HTTPException(422, '清晰化环境缺少节点或 RealESRGAN_x2plus.pth 模型')
    task_id = str(uuid.uuid4())
    asset_id = f'HD-{task_id[:8]}'
    prefix = f'导演工作台工作目录/{project_id}/enhancement/{task_id}'
    if facts['kind'] == 'image':
        graph = enhancement.image_graph(str(source), prefix)
        workflow = backend.task_snapshot_root(project_id) / f'enhancement-{task_id}.json'
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(json.dumps(graph, ensure_ascii=False), encoding='utf-8')
    else:
        workflow = backend.build_finish_workflow_snapshot(project_id, asset_id, source, enhancement.MODEL, None, 'uniform', 0)
    payload = {'kind': 'workflow', 'pipeline_stage_id': 'media-enhancement',
               'project_id': project_id, 'asset_id': asset_id, 'workflow': str(workflow),
               'enhancement': {'source': request.source, 'source_facts': facts, 'model': enhancement.MODEL, 'scale': 2},
               'submission_receipt': {'key': request.idempotency_key, 'request': submission}}
    try:
        payload = backend.freeze_task_payload(payload, project_id=project_id)
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (stamp.st_size, stamp.st_mtime_ns):
            raise ValueError('素材在检查期间发生变化，请重新提交')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f'无法冻结清晰化输入: {exc}') from exc
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, asset_id, 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id) or {'id': task_id, 'status': 'preparing'}


def describe_enhancement_result(payload: dict, done: dict) -> dict:
    outputs = done.get('published_outputs') or [f'output/{v}' for v in done.get('outputs', [])]
    if len(outputs) != 1:
        raise ValueError('清晰化回执没有唯一输出，请核对任务')
    facts = backend.authorized_media_facts(outputs[0])
    source = payload['enhancement']['source_facts']
    if not facts.get('known') or (facts.get('width'), facts.get('height')) != (source['width'] * 2, source['height'] * 2):
        raise ValueError('清晰化输出尺寸与 2× 目标不符，请核对结果')
    if source['kind'] == 'video':
        if (facts.get('frames') != source['frames'] or facts.get('audio_streams') != source['audio_streams']
                or not facts.get('fps') or abs(facts['fps'] - source['fps']) > 0.1):
            raise ValueError('清晰化输出帧数、帧率或音轨与原视频不符，请核对结果')
        if source.get('duration_seconds') and (not facts.get('duration_seconds') or
                abs(facts['duration_seconds'] - source['duration_seconds']) > 2 / source['fps']):
            raise ValueError('清晰化输出时长与原视频不符，请核对结果')
    return {**done, 'enhancement': payload['enhancement'], 'output_facts': facts, 'candidate_status': 'pending_review'}


def find_submission_task(project_id: str, key: str) -> dict[str, Any] | None:
    with backend.DB_LOCK, backend.db() as connection:
        row = connection.execute(
            "SELECT tasks.* FROM tasks JOIN task_idempotency ON task_idempotency.task_id = tasks.id "
            "WHERE task_idempotency.project_id = ? AND task_idempotency.idempotency_key = ?",
            (project_id, key),
        ).fetchone()
    return backend.task_row(row) if row else None


@ROUTER.get("/api/projects/{project_id}/submission-receipt")
def submission_receipt(project_id: str, key: str = Query(min_length=1, max_length=160)) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    task = backend.find_submission_task(project_id, key)
    if not task:
        raise HTTPException(404, {'code': 'submission_not_found', 'message': '尚未找到已落库任务；原请求可能仍在处理中，请勿换请求标识重复提交', 'retryable': True})
    return task


@backend.plan_transaction
def submit_project_task(request: StartRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else backend.CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else backend.CURRENT_PROJECT_ID
    submission_request = request.model_dump(exclude={'idempotency_key'}, mode='json')
    if request.expected_revision is None:
        submission_request.pop('expected_revision', None)
    if request.idempotency_key is not None:
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission_request:
                raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '该请求标识已用于不同生成参数；请查询原任务或为新操作使用新标识', 'task_id': existing['id'], 'retryable': False})
            return existing
    plan_path = backend.resolve_registered_path(str(project['plan_path'])) if manifest is not None else backend.PLAN_PATH
    templates = project.get('task_templates', {}) if manifest is not None else backend.TASK_TEMPLATES
    backend.require_submission_capacity()
    asset_id = request.asset_id
    backend.ensure_asset_not_pending(project_id, asset_id)
    if not asset_id or asset_id == str(project.get("assembly_asset_id", "")):
        raise HTTPException(400, "当前素材不能作为单段生成任务；完整版请使用装配入口")
    payload = dict(templates.get(asset_id, {}))
    payload["asset_id"] = asset_id
    try:
        document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        document = {"segments": []}
    backend.check_plan_revision(document, request.expected_revision)
    if request.pipeline_stage_id == 'auto':
        raise HTTPException(422, {'code': 'explicit_stage_required',
                                  'message': '自动制作方案已移除；请显式选择制作步骤或工作流后再提交。'})
    segment = next((item for item in document.get("segments", []) if isinstance(item, dict) and item.get("id") == asset_id), None)
    if isinstance(segment, dict) and segment.get("workflow") and not payload.get("workflow"):
        payload["workflow"] = str(segment["workflow"])
    payload['workflow'], stage_id = backend.select_preset_execution(project, payload.get('workflow'), request.pipeline_stage_id)
    if not payload.get("workflow"):
        raise HTTPException(422, {
            'code': 'workflow_required',
            'message': f'{asset_id} 尚未明确绑定可执行工作流或制作方案；请先保存分镜方案后再提交。',
            'blockers': [{
                'code': 'workflow_required', 'object': {'kind': 'segment', 'id': asset_id},
                'action': {'id': 'choose_workflow', 'label': '选择制作方案', 'method': 'PATCH',
                           'path': f'/api/projects/{project_id if manifest is not None else backend.CURRENT_PROJECT_ID}/plan/segments/{asset_id}'},
            }],
        })
    if stage_id:
        values = dict(request.pipeline_values)
        if request.prompt is not None:
            values["prompt"] = request.prompt
        if request.seed is not None and "seed" not in values:
            values["seed"] = request.seed
        payload = backend.prepare_pipeline_payload(payload, stage_id, values, manifest)
    else:
        if request.prompt is not None:
            if payload.get("workflow"):
                payload["prompt_override"] = request.prompt
            else:
                payload["prompt"] = request.prompt
        elif payload.get("workflow") and isinstance(segment, dict) and segment.get("prompt"):
            payload["prompt_override"] = str(segment["prompt"])
        if request.seed is not None:
            payload["seed"] = request.seed
    if isinstance(segment, dict):
        payload["segment_snapshot"] = backend.segment_generation_snapshot(segment)
        payload['segment_dependencies'] = list(segment.get('dependencies') or [])
    payload.update({"project_id": project_id, "plan_path": str(plan_path), "assembly_asset_id": str(project.get("assembly_asset_id", ""))})
    if request.idempotency_key is not None:
        payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission_request}
    try:
        payload = backend.freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"无法冻结本次执行快照: {exc}") from exc
    task_id = str(uuid.uuid4())
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, asset_id, "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id) or {"id": task_id, "status": "preparing"}
