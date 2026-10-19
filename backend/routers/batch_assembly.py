"""批次与合成：批次启动/停止/恢复，合成编辑与合成任务。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
import time
from typing import Any
import uuid
from fastapi import APIRouter, HTTPException
from .. import assembly_edit, task_scheduler
from ..schemas import AssemblyJoinReviewRequest, AssemblyRequest, AssemblySoundRequest, BatchRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.post("/api/batches")
@backend.gpu_admission
def start_batch(request: BatchRequest) -> dict[str, Any]:
    return backend.submit_project_batch(request)


@ROUTER.post("/api/projects/{project_id}/batches")
@backend.gpu_admission
def start_project_batch(project_id: str, request: BatchRequest) -> dict[str, Any]:
    return backend.submit_project_batch(request, backend.require_project_manifest(project_id))


@backend.plan_transaction
def submit_project_batch(request: BatchRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else backend.CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else backend.CURRENT_PROJECT_ID
    submission_request = request.model_dump(exclude={'idempotency_key'}, mode='json')
    if request.expected_revision is None:
        submission_request.pop('expected_revision', None)
    if request.idempotency_key:
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission_request or not existing.get('batch_id'):
                raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '请求标识已用于不同参数', 'retryable': False})
            with backend.DB_LOCK, backend.db() as connection:
                rows = connection.execute('SELECT id FROM tasks WHERE batch_id=? ORDER BY sequence', (existing['batch_id'],)).fetchall()
            return {'batch_id': existing['batch_id'], 'task_ids': [row['id'] for row in rows], 'status': 'accepted'}
    plan_path = backend.resolve_registered_path(str(project['plan_path'])) if manifest is not None else backend.PLAN_PATH
    backend.require_submission_capacity()
    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"无法读取分段计划: {exc}") from exc
    backend.check_plan_revision(plan_document, request.expected_revision)
    plan_segments = {str(item.get("id")): item for item in plan_document.get("segments", []) if isinstance(item, dict) and item.get("id")}
    requested_ids = list(request.asset_ids)
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(422, "批次不能重复包含同一个分镜")
    unsupported = sorted(set(requested_ids) - set(plan_segments))
    if unsupported:
        raise HTTPException(400, f"计划中不存在这些片段: {', '.join(unsupported)}")
    batch_positions = {asset_id: index for index, asset_id in enumerate(requested_ids)}
    for asset_id in requested_ids:
        later_dependencies = [str(dependency) for dependency in plan_segments[asset_id].get('dependencies', [])
                              if str(dependency) in batch_positions and
                              batch_positions[str(dependency)] >= batch_positions[asset_id]]
        if later_dependencies:
            raise HTTPException(422, {'code': 'batch_dependency_order', 'asset_id': asset_id,
                                      'dependencies': later_dependencies,
                                      'message': '请将批次中的前置分镜放在依赖它的分镜之前',
                                      'action': 'reorder_batch'})
    for asset_id in requested_ids:
        backend.ensure_asset_not_pending(project_id, asset_id)
    if request.pipeline_stage_id == 'auto':
        raise HTTPException(422, {'code': 'explicit_stage_required',
                                  'message': '自动制作方案已移除；请显式选择制作步骤或工作流后再提交批次。'})
    batch_id = str(uuid.uuid4())
    now = time.time()
    task_ids: list[str] = []
    payloads: list[dict[str, Any]] = []
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner(), len(requested_ids))
        for sequence, asset_id in enumerate(requested_ids, start=1):
            segment = plan_segments[asset_id]
            workflow = segment.get("workflow")
            if not workflow and request.pipeline_stage_id is not None:
                workflow, _ = backend.select_preset_execution(project, None, request.pipeline_stage_id)
            if not workflow:
                raise HTTPException(422, {
                    'code': 'workflow_required',
                    'message': f'{asset_id} 尚未明确绑定可执行工作流或制作方案；批次未创建。',
                    'blockers': [{
                        'code': 'workflow_required', 'object': {'kind': 'segment', 'id': asset_id},
                        'action': {'id': 'choose_workflow', 'label': '选择制作方案', 'method': 'PATCH',
                                   'path': f'/api/projects/{project_id}/plan/segments/{asset_id}'},
                    }],
                })
            payload = {
                "asset_id": asset_id,
                "workflow": str(workflow),
                "project_id": project_id,
                "plan_path": str(plan_path),
                "assembly_asset_id": str(project.get("assembly_asset_id", "")),
                "segment_snapshot": backend.segment_generation_snapshot(segment),
            }
            prompt = request.prompt if request.prompt is not None else segment.get("prompt")
            payload['workflow'], stage_id = backend.select_preset_execution(project, payload.get('workflow'), request.pipeline_stage_id)
            if stage_id:
                values = dict(request.pipeline_values_by_asset.get(asset_id, {}))
                if prompt is not None and "prompt" not in values:
                    values["prompt"] = str(prompt)
                payload = backend.prepare_pipeline_payload(payload, stage_id, values, manifest)
            elif prompt is not None:
                payload["prompt_override"] = str(prompt)
            try:
                payload = backend.freeze_task_payload(payload, project_id=project_id)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(422, f"{asset_id} 无法冻结执行快照: {exc}") from exc
            task_id = str(uuid.uuid4())
            if request.idempotency_key:
                payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission_request}
            payload['segment_dependencies'] = list(segment.get('dependencies') or [])
            task_ids.append(task_id); payloads.append(payload)
            connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, stop_requested, payload, batch_id, sequence, project_id) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)", (task_id, asset_id, "batch_waiting", now, now, json.dumps(payload, ensure_ascii=False), batch_id, sequence, project_id))
        connection.commit()
    backend.TASK_SCHEDULER.wake()
    return {"batch_id": batch_id, "task_ids": task_ids, "status": "accepted"}


@ROUTER.get('/api/projects/{project_id}/assembly/edit')
def get_project_assembly_edit(project_id: str) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    try:
        return backend.assembly_edit_view(manifest, document)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.put('/api/projects/{project_id}/assembly/joins/{left_id}/{right_id}')
@backend.plan_transaction
def save_project_assembly_join(project_id: str, left_id: str, right_id: str,
                               request: AssemblyJoinReviewRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_revision)
    view = backend.assembly_edit_view(manifest, document, include_continuity=False)
    join = next((item for item in view['joins']
                 if item['left_id'] == left_id and item['right_id'] == right_id), None)
    if join is None:
        raise HTTPException(422, '所选分镜不是当前计划中相邻的接点')
    if request.status == 'approved' and not join['review_ready']:
        raise HTTPException(422, '接点两侧均需有当前视频才能审核通过')
    edit = document.setdefault('assembly_edit', {})
    edit.setdefault('joins', {})[join['key']] = {
        'status': request.status, 'constraint': request.constraint.strip(),
        'note': request.note.strip(), 'signature': join['signature'],
        'reviewed_at': time.time(),
    }
    if request.status != 'approved' and isinstance(document.get('assembly'), dict) and (document['assembly'] or {}).get('output'):
        document['assembly'].update(status='stale', notes='接点审核要求修改，旧成片保留但需重新装配')
    backend.write_plan_version(plan_path, document)
    return backend.assembly_edit_view(manifest, document, include_continuity=False, readiness_complete=False)


@ROUTER.put('/api/projects/{project_id}/assembly/sound')
@backend.plan_transaction
def save_project_assembly_sound(project_id: str, request: AssemblySoundRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_revision)
    if request.policy == 'complete_master' and not request.audio_asset_id:
        raise HTTPException(422, '统一音轨须选择当前作品已登记的音频素材')
    if request.policy in {'segment_native', 'mute'} and request.audio_asset_id:
        raise HTTPException(422, '保留片段原音或静音时不能指定全片音轨')
    edit = document.setdefault('assembly_edit', {})
    edit['sound'] = {'policy': request.policy, 'audio_asset_id': request.audio_asset_id}
    try:
        sound = backend.assembly_sound_selection(manifest, document)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    backend.persist_plan_edit(project_id, document, plan_path)
    return backend.assembly_edit_view(manifest, document, include_continuity=False,
                                      sound_override=sound, readiness_complete=False)


@ROUTER.get('/api/projects/{project_id}/assembly/preflight')
def preflight_project_assembly(project_id: str) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    try:
        sound = backend.assembly_sound_selection(manifest, document)
        view = backend.assembly_edit_view(manifest, document, include_continuity=False,
                                          sound_override=sound, readiness_complete=True)
    except ValueError as exc:
        view = backend.assembly_edit_view(manifest, document, include_continuity=False,
                                          sound_override={'policy': 'segment_native', 'audio_asset_id': None,
                                                          'path': None, 'duration_seconds': None, 'error': str(exc)},
                                          readiness_complete=True)
    builder_blocker = backend.assembly_builder_blocker(manifest)
    if builder_blocker:
        view['blockers'].append(builder_blocker)
    view['ready'] = bool(document.get('segments')) and not view['blockers'] and view['ready']
    return view


def assembly_video_blockers(document):
    blockers = []
    for segment in document.get('segments', []):
        if not isinstance(segment, dict):
            continue
        asset_id = str(segment.get('id'))
        reference = (segment.get('video') or {}).get('path')
        try:
            exists = bool(reference and backend.resolve_registered_path(str(reference)).is_file())
        except (HTTPException, ValueError, OSError):
            exists = False
        if not exists:
            blockers.append({'code': 'video_required', 'message': f'{asset_id} 当前生成视频不存在', 'dependency': asset_id})
        current = segment.get('current_version_task_id')
        version = next((item for item in segment.get('generation_history', []) if item.get('task_id') == current), None) if current else None
        saved_duration = (version or {}).get('snapshot', {}).get('duration_seconds')
        if saved_duration is not None and float(saved_duration) != float(segment.get('duration_seconds', 0)):
            blockers.append({'code': 'video_duration_stale', 'message': f'{asset_id} 当前视频按旧时长 {saved_duration} 秒生成；请先重新生成或恢复匹配的分镜时长', 'dependency': asset_id})
    return blockers


@ROUTER.post("/api/assembly")
@backend.gpu_admission
def start_assembly(request: AssemblyRequest | None = None) -> dict[str, Any]:
    return backend.submit_project_assembly(request=request)


@ROUTER.post("/api/projects/{project_id}/assembly")
@backend.gpu_admission
def start_project_assembly(project_id: str, request: AssemblyRequest | None = None) -> dict[str, Any]:
    return backend.submit_project_assembly(backend.require_project_manifest(project_id), request)


@backend.plan_transaction
def submit_project_assembly(manifest: dict[str, Any] | None = None, request: AssemblyRequest | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else backend.CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else backend.CURRENT_PROJECT_ID
    request = request or AssemblyRequest()
    submission = {'kind': 'assembly', 'expected_revision': request.expected_revision}
    if request.idempotency_key:
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
    plan_path = backend.resolve_registered_path(str(project['plan_path'])) if manifest is not None else backend.PLAN_PATH
    stages = project.get('pipeline', []) if manifest is not None else backend.PIPELINE_STAGES
    backend.require_submission_capacity()
    plan_document, _ = backend.load_project_plan(project_id)
    backend.check_plan_revision(plan_document, request.expected_revision)
    builder_blocker = backend.assembly_builder_blocker(manifest)
    if builder_blocker:
        raise HTTPException(422, {'code': builder_blocker['code'], 'message': builder_blocker['message'], 'blockers': [builder_blocker]})
    blocking = assembly_edit.blocking_joins(plan_document)
    if blocking:
        raise HTTPException(409, f"接点审核未通过或已过期: {', '.join(blocking)}")
    adoption_blockers = backend.assembly_adoption_blockers(project, plan_document)
    if adoption_blockers:
        raise HTTPException(409, {
            'code': 'assembly_adoption_required',
            'message': '所有参与装配的分镜都必须明确采用当前候选后才能装配。',
            'blockers': adoption_blockers,
        })
    segment_ids = [str(item.get("id")) for item in plan_document.get("segments", []) if isinstance(item, dict) and item.get("id")]
    if not segment_ids:
        raise HTTPException(422, "装配需要至少一个分镜")
    missing_video = [asset_id for asset_id in segment_ids if not next(
        ((item.get("video") or {}).get("path") for item in plan_document["segments"]
         if isinstance(item, dict) and str(item.get("id")) == asset_id), None
    )]
    if missing_video:
        raise HTTPException(409, f"{len(missing_video)} 个分镜尚无当前生成视频")
    try:
        workflow_path = (backend.build_assembly_snapshot(manifest, bind_adoptions=True)
                         if manifest is not None else backend.build_assembly_snapshot())
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"无法根据最新分段计划生成装配工作流: {exc}") from exc
    active_statuses = tuple(task_scheduler.WAITING | task_scheduler.EXECUTING)
    assembly_asset_id = str(project.get("assembly_asset_id", "MASTER"))
    placeholders = ",".join("?" for _ in active_statuses)
    with backend.DB_LOCK, backend.db() as connection:
        active = connection.execute(f"SELECT id FROM tasks WHERE project_id = ? AND asset_id = ? AND status IN ({placeholders})", (project_id, assembly_asset_id, *active_statuses)).fetchone()
    if active:
        raise HTTPException(409, "完整版装配已经在运行")
    task_id = str(uuid.uuid4())
    now = time.time()
    payload = {"asset_id": assembly_asset_id, "workflow": str(workflow_path), "source_plan": str(plan_path), "plan_path": str(plan_path), "assembly_asset_id": assembly_asset_id, "project_id": project_id,
               "assembly_edit": {'joins': assembly_edit.joins(plan_document), 'sound': backend.assembly_sound_selection(project, plan_document)}}
    previous_assembly = plan_document.get('assembly') or {}
    payload['previous_assembly_candidate'] = {key: previous_assembly.get(key) for key in ('prompt_id', 'output')}
    if request.idempotency_key:
        payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission}
    try:
        payload = backend.freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"无法冻结装配执行快照: {exc}") from exc
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, assembly_asset_id, "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id) or {"id": task_id, "asset_id": assembly_asset_id, "status": "preparing"}


@ROUTER.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str) -> dict[str, Any]:
    return backend.stop_project_batch_tasks(backend.CURRENT_PROJECT_ID, batch_id)


@ROUTER.post("/api/projects/{project_id}/batches/{batch_id}/stop")
def stop_project_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    return backend.stop_project_batch_tasks(project_id, batch_id)


def stop_project_batch_tasks(project_id: str, batch_id: str) -> dict[str, Any]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT id,status FROM tasks WHERE project_id=? AND batch_id=?", (project_id, batch_id)).fetchall()
        if not rows:
            raise HTTPException(404, "批次不存在")
        if any(row['status'] == 'needs_reconcile' for row in rows):
            raise HTTPException(409, "批次有待核对任务，请先核对原回执")
        connection.execute("UPDATE tasks SET status='stopped',stop_requested=1,updated_at=?,error='批次已停止，未提交到 ComfyUI' WHERE project_id=? AND batch_id=? AND status='batch_waiting'", (time.time(), project_id, batch_id))
        connection.execute("UPDATE tasks SET status='stop_requested',stop_requested=1,updated_at=? WHERE project_id=? AND batch_id=? AND status IN ('preparing','submitting','queued','running','stopping','stop_requested')", (time.time(), project_id, batch_id))
        connection.commit()
        updated = connection.execute("SELECT * FROM tasks WHERE project_id=? AND batch_id=? ORDER BY sequence", (project_id, batch_id)).fetchall()
    return {"batch_id": batch_id, "tasks": [backend.task_row(row) for row in updated]}


@ROUTER.post("/api/batches/{batch_id}/resume")
@backend.gpu_admission
def resume_batch(batch_id: str) -> dict[str, Any]:
    return backend.resume_project_batch_tasks(backend.CURRENT_PROJECT_ID, batch_id)


@ROUTER.post("/api/projects/{project_id}/batches/{batch_id}/resume")
@backend.gpu_admission
def resume_project_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    return backend.resume_project_batch_tasks(project_id, batch_id, backend.require_project_manifest(project_id))


def resume_project_batch_tasks(project_id: str, batch_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? AND batch_id = ? ORDER BY sequence", (project_id, batch_id)).fetchall()
    if not rows:
        raise HTTPException(404, "批次不存在")
    for row in rows:
        if row["status"] == "needs_reconcile" or (row["status"] in {"stopped", "failed"} and row["prompt_id"]):
            backend.reconcile_task(backend.task_in_project_context(backend.task_row(row), manifest) if manifest is not None else backend.task_row(row))
        elif row["status"] not in {"succeeded", "failed", "stopped"}:
            raise HTTPException(409, "批次仍在运行或停止中，不能重复恢复")
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? AND batch_id = ? ORDER BY sequence", (project_id, batch_id)).fetchall()
    pending = [row for row in rows if row["status"] != "succeeded"]
    if not pending:
        raise HTTPException(409, "批次已经全部完成，无需恢复")
    backend.require_submission_capacity()
    new_batch_id = str(uuid.uuid4())
    now = time.time()
    task_ids: list[str] = []
    payloads = [(backend.task_in_project_context(backend.task_row(row), manifest)['payload'] if manifest is not None else json.loads(row['payload'])) for row in pending]
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner(), len(pending))
        for sequence, row in enumerate(pending, start=1):
            new_id = str(uuid.uuid4())
            task_ids.append(new_id)
            connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, stop_requested, payload, batch_id, sequence, project_id) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)", (new_id, row["asset_id"], "batch_waiting", now, now, json.dumps(payloads[sequence - 1], ensure_ascii=False), new_batch_id, sequence, row["project_id"]))
        connection.commit()
    backend.TASK_SCHEDULER.wake()
    return {"batch_id": new_batch_id, "task_ids": task_ids, "resumed_from": batch_id, "status": "started"}
