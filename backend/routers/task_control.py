"""任务控制与恢复：队列状态、停止与资源释放、缺失执行兜底、断点恢复。

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
from fastapi import APIRouter, HTTPException
from urllib.error import HTTPError, URLError
from .. import audio_edit_tasks, audio_master_tasks, background_jobs, keyframes, media_operations, speech, task_scheduler
from ..schemas import MissingExecutionResolution
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/queue')
def queue_status():
    principal = backend.current_principal.get()
    if principal is None or principal.administrator:
        return backend.TASK_SCHEDULER.summary()
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks ORDER BY created_at DESC').fetchall()
    own = [backend.task_row(row) for row in rows if json.loads(row['payload']).get('owner_user') == principal.identity.username]
    return {'gpu_slots': 1, 'cpu_slots': backend.TASK_SCHEDULER.cpu_workers, 'global_limit': backend.TASK_SCHEDULER.global_limit,
            'user_limit': backend.TASK_SCHEDULER.user_limit,
            'tasks': [{'id': task['id'], 'status': task['status'], 'queue': backend.TASK_SCHEDULER.queue_info(task['id'])} for task in own if task['status'] not in task_scheduler.TERMINAL]}


@ROUTER.post("/api/resources/release")
def release_resources() -> dict[str, Any]:
    """Compatibility endpoint; the UI relies on automatic idle release."""
    with backend.GPU_ADMISSION_LOCK:
        try:
            reason = backend.gpu_idle_reason()
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise HTTPException(503, f"无法确认 GPU 空闲状态: {exc}") from exc
        if reason:
            raise HTTPException(409, reason)
        try:
            backend.request_comfy_free()
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise HTTPException(502, f"ComfyUI 回收请求失败: {exc}") from exc
        backend.GPU_IDLE_RELEASER.note_manual_release()
    time.sleep(0.4)
    return {"released": True, "comfy": backend.comfy_status(), "message": "已请求 ComfyUI 回收空闲模型和缓存"}


@ROUTER.post("/api/tasks/{task_id}/stop")
def stop(task_id: str) -> dict[str, Any]:
    task = backend.get_current_project_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return backend.request_task_stop(task)


@ROUTER.post("/api/projects/{project_id}/tasks/{task_id}/stop")
def stop_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    return backend.request_task_stop(backend.project_task_detail(project_id, task_id))


def request_task_stop(task: dict[str, Any]) -> dict[str, Any]:
    if backend.TASK_SCHEDULER.cancel_pending(task['id']):
        if task['payload'].get('kind') == 'reference_semantic':
            background_jobs.update(backend.analysis_job_root(), task['payload'], status='stopped')
        return backend.get_task(task['id']) or task
    if task['payload'].get('kind') == 'media_operation':
        with backend.MEDIA_OPERATION_LOCK:
            task = backend.get_task(task['id']) or task
            if task['status'] == 'needs_reconcile':
                raise HTTPException(409, '请先核对媒体执行回执，不能把未知执行标记为已停止')
            if task['status'] not in {'succeeded', 'failed', 'stopped'}:
                backend.set_task(task['id'], status='stop_requested', stop_requested=1,
                         error='正在请求停止媒体处理子进程，完成后才标记停止')
            return backend.get_task(task['id']) or task
    if task['payload'].get('kind') in {'audio_edit', 'audio_master'}:
        with backend.AUDIO_EDIT_LOCK:
            task = backend.get_task(task['id']) or task
            if task['status'] == 'needs_reconcile':
                raise HTTPException(409, '请先核对CPU音频回执，不能把未知执行标记为已停止')
            if task['status'] not in {'succeeded', 'failed', 'stopped'}:
                backend.set_task(task['id'], status='stop_requested', stop_requested=1,
                         error='已请求停止；CPU处理无法中途取消，结束后不登记候选')
            return backend.get_task(task['id']) or task
    task_id = task['id']
    if task['status'] == 'needs_reconcile':
        raise HTTPException(409, '原任务待核对，请先核对原回执；不能把未知任务标记为已停止')
    if task["status"] in {"succeeded", "failed", "stopped"}:
        return task
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute("UPDATE tasks SET stop_requested=1,status='stop_requested',updated_at=? WHERE id=? AND status NOT IN ('succeeded','failed','stopped','needs_reconcile')", (time.time(), task_id))
        connection.commit()
    return backend.get_task(task_id) or task


@ROUTER.post("/api/projects/{project_id}/tasks/{task_id}/resolve-missing")
@backend.gpu_admission
def resolve_missing_execution(project_id: str, task_id: str, request: MissingExecutionResolution) -> dict[str, Any]:
    """Record an operator's lost-execution conclusion without submitting a retry."""
    task = backend.project_task_detail(project_id, task_id)
    if task['payload'].get('kind') in {'reference_semantic', 'reference_decomposition'}:
        raise HTTPException(409, {'code': 'external_execution_unresolved', 'message': '参考进程不属于 ComfyUI；必须核对独立完成回执，不能依据 ComfyUI 空队列解除占用'})
    note = request.note.strip()
    if not note:
        raise HTTPException(422, '请填写原执行已结束及输出检查的核对说明')
    if task['payload'].get('kind') == 'media_operation':
        with backend.MEDIA_OPERATION_LOCK:
            task = backend.project_task_detail(project_id, task_id)
            if task['status'] != 'needs_reconcile':
                raise HTTPException(409, '仅支持待核对媒体任务')
            if backend.audio_edit_owner_alive(task['payload']):
                raise HTTPException(409, '原执行服务仍存活，不能确认旧执行结束')
            try:
                worker_alive = media_operations.owner_alive(task['payload'])
            except (OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(409, f'无法确认媒体子进程结束: {exc}') from exc
            if worker_alive:
                raise HTTPException(409, '媒体子进程仍存活，不能释放执行名额')
            try:
                media_operations.read_receipt(task['payload'])
            except (OSError, ValueError, media_operations.MediaOperationFailed):
                pass
            else:
                raise HTTPException(409, '已有有效媒体回执，请核对原回执恢复结果')
            result = dict(task.get('result') or {})
            result['missing_execution_resolution'] = {'confirmed_at': time.time(), 'note': note,
                'execution_owner': task['payload'].get('execution_owner'), 'execution_verified_ended': True,
                'source': 'operator_confirmation', 'kind': 'media_operation'}
            backend.set_task(task_id, status='failed', stop_requested=0, result=json.dumps(result, ensure_ascii=False),
                     error='原媒体执行已结束且无可恢复回执；已确认，未重新执行')
            return backend.get_task(task_id)
    if task['payload'].get('kind') in {'audio_edit', 'audio_master'}:
        with backend.AUDIO_EDIT_LOCK:
            task = backend.project_task_detail(project_id, task_id)
            if task['status'] != 'needs_reconcile':
                raise HTTPException(409, '仅支持待核对CPU音频编辑任务')
            owner_alive = backend.audio_master_owner_alive(task['payload']) if task['payload'].get('kind') == 'audio_master' else backend.audio_edit_owner_alive(task['payload'])
            if owner_alive:
                raise HTTPException(409, '原CPU执行服务仍存活，不能确认旧执行结束')
            try:
                (audio_master_tasks if task['payload'].get('kind') == 'audio_master' else audio_edit_tasks).read_receipt(task['payload'])
            except (OSError, ValueError):
                pass
            else:
                raise HTTPException(409, '已有有效CPU回执，请核对原回执恢复结果')
            result = dict(task.get('result') or {})
            result['missing_execution_resolution'] = {'confirmed_at': time.time(), 'note': note,
                'execution_owner': task['payload'].get('execution_owner'), 'execution_verified_ended': True,
                'source': 'operator_confirmation', 'kind': task['payload'].get('kind')}
            backend.set_task(task_id, status='failed', stop_requested=0, result=json.dumps(result, ensure_ascii=False),
                     error='CPU执行已结束且无可恢复回执；已人工确认，未重新执行')
            return backend.get_task(task_id)
    if task['status'] != 'needs_reconcile' or task['payload'].get('kind') == 'speech':
        raise HTTPException(409, '仅支持缺失 ComfyUI 历史的待核对任务；声音任务请核对独立执行器')
    try:
        queue = backend.request_json('/queue')
        history = backend.request_json(f"/history/{task['prompt_id']}") if task.get('prompt_id') else {}
    except (OSError, ValueError, HTTPError, URLError) as exc:
        raise HTTPException(503, '无法核实原执行状态，请恢复连接后重试') from exc
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise HTTPException(503, '队列回执无效，不能结束核对')
    if queue['queue_running'] or queue['queue_pending']:
        raise HTTPException(409, '队列仍有任务，不能确认原执行已结束')
    if not isinstance(history, dict) or history:
        raise HTTPException(409, '原历史存在或回执无效，请使用核对原回执恢复结果')
    result = dict(task.get('result') or {})
    result['missing_execution_resolution'] = {
        'confirmed_at': time.time(), 'note': note, 'prompt_id': task.get('prompt_id'),
        'queue_verified_empty': True, 'history_verified_missing': True,
        'source': 'operator_confirmation',
    }
    with backend.DB_LOCK, backend.db() as connection:
        changed = connection.execute(
            "UPDATE tasks SET status='failed',stop_requested=0,error=?,result=?,updated_at=? WHERE id=? AND project_id=? AND status='needs_reconcile'",
            ('原执行历史缺失；已人工确认执行结束，未自动重跑', json.dumps(result, ensure_ascii=False), time.time(), task_id, project_id),
        ).rowcount
        connection.commit()
    if not changed:
        raise HTTPException(409, '任务状态已变化，请刷新后检查')
    return backend.get_task(task_id) or task


@ROUTER.post("/api/projects/{project_id}/tasks/{task_id}/reconcile")
@backend.gpu_admission
def reconcile_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Only reconcile the original execution; never create a retry."""
    task = backend.project_task_detail(project_id, task_id)
    if task['status'] == 'succeeded':
        return task
    if task['status'] not in {'needs_reconcile', 'failed', 'stopped'}:
        raise HTTPException(409, '任务仍受执行器监控，请查询状态而非重复核对')
    return backend.reconcile_task(backend.task_in_project_context(task, backend.require_project_manifest(project_id)))


def reconcile_task(task: dict[str, Any]) -> dict[str, Any]:
    if task['payload'].get('kind') == 'reference_semantic':
        backend.reconcile_semantic_job(task['project_id'], task['payload']['analysis_id'])
        return backend.get_task(task['id'])
    if task['payload'].get('kind') == 'reference_decomposition':
        payload = task['payload']
        try:
            receipt = json.loads(Path(payload['receipt_path']).read_text(encoding='utf-8'))
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(409, {'code': 'reference_receipt_unknown', 'message': '参考拆分完成回执尚未核实；不会重新执行'}) from exc
        expected = {'task_id': task['id'], 'source': str(Path(payload['source']).resolve()), 'analysis_path': str(Path(payload['analysis_path']).resolve()), 'status': 'succeeded'}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise HTTPException(409, {'code': 'reference_receipt_mismatch', 'message': '参考完成回执与冻结输入不匹配'})
        document, _ = backend.read_reverse_analysis(task['project_id'], payload['analysis_id'])
        backend.set_task(task['id'], status='succeeded', error=None, result=json.dumps(document, ensure_ascii=False))
        backend.TASK_SCHEDULER.wake()
        return backend.get_task(task['id'])
    """Recover a restart receipt without assuming an unknown submission failed."""
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    if task['payload'].get('kind') == 'media_operation':
        if task['status'] == 'stopped':
            return task
        try:
            receipt = media_operations.read_receipt(task['payload'])
        except (OSError, ValueError, media_operations.MediaOperationFailed) as exc:
            raise HTTPException(409, f'媒体回执尚未核实，不会重新执行: {exc}') from exc
        return backend.complete_media_operation_task(task, receipt)
    if task['payload'].get('kind') == 'audio_edit':
        if task['status'] == 'stopped':
            return task
        try:
            receipt = audio_edit_tasks.read_receipt(task['payload'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'CPU音频编辑回执尚未核实，不会重新执行: {exc}') from exc
        return backend.complete_audio_edit_task(task, receipt)
    if task['payload'].get('kind') == 'audio_master':
        if task['status'] == 'stopped':
            return task
        try:
            receipt = audio_master_tasks.read_receipt(task['payload'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'CPU全片音频后期回执尚未核实，不会重新执行: {exc}') from exc
        return backend.complete_audio_master_task(task, receipt)
    if task['payload'].get('kind') == 'speech':
        try:
            receipt = speech.read_receipt(task['payload'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'声音完成回执尚未核实，不会重新生成: {exc}') from exc
        return backend.complete_speech_task(task, receipt)
    prompt_id = task.get("prompt_id")
    if not prompt_id:
        raise HTTPException(409, "该任务没有提交回执，无法确认是否已生成；已阻止重复提交")
    try:
        queue = backend.request_json("/queue")
    except (OSError, ValueError, HTTPError, URLError) as exc:
        raise HTTPException(503, "暂时无法核对 ComfyUI 队列，请恢复连接后重试") from exc
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ("queue_running", "queue_pending")):
        raise HTTPException(503, "ComfyUI 队列回执无效，尚未恢复任务")
    for item in queue["queue_running"] + queue["queue_pending"]:
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            raise HTTPException(409, "原任务仍在 ComfyUI 队列中；请等待完成后再次核对，不会重复生成")
    if task['payload'].get('kind') == 'keyframe':
        try:
            done = keyframes.read_result(backend.request_json(f'/history/{prompt_id}'), str(prompt_id), task['payload'], backend.OUTPUT_ROOT)
        except Exception as exc:
            raise HTTPException(409, f'关键帧结果尚未核实，不会重新生成: {exc}') from exc
        if not done:
            raise HTTPException(409, '尚未找到关键帧完成回执，不会重新生成')
        if done['status'] == 'error':
            backend.set_task(task['id'], status='failed', result=json.dumps(done, ensure_ascii=False), error=backend.comfy_execution_error(task['payload'], done))
            return backend.get_task(task['id']) or task
        return backend.complete_keyframe_task(task, done)
    done = (backend.history_done(str(prompt_id), include_images=True)
            if task['payload'].get('pipeline_stage_id') == 'media-enhancement' else backend.history_done(str(prompt_id)))
    if not done:
        raise HTTPException(409, "尚未找到原任务的完成记录，可能仍在执行或历史已清理；已阻止重复提交")
    done = backend.attach_h3_duration_result(done, task['payload'])
    if done["status"] == "error":
        backend.set_task(task["id"], status="failed", result=json.dumps(done, ensure_ascii=False), error=backend.comfy_execution_error(task["payload"], done))
        return backend.get_task(task["id"]) or task
    outputs = done.get("outputs", [])
    if not outputs:
        raise HTTPException(409, "原任务报告成功，但没有可恢复的视频输出；不会重新生成")
    for output in outputs:
        path = (backend.OUTPUT_ROOT / str(output)).resolve()
        if not path.is_relative_to(backend.OUTPUT_ROOT.resolve()) or not path.is_file():
            raise HTTPException(409, "原任务报告成功，但输出文件缺失或路径无效；请先核对输出文件")
    payload = task["payload"]
    target_plan = Path(str(payload.get("plan_path"))) if payload.get("plan_path") else backend.PLAN_PATH
    done = backend.publish_task_outputs(task, done)
    if payload.get('pipeline_stage_id') == 'media-enhancement':
        try:
            done = backend.describe_enhancement_result(payload, done)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, f'清晰化结果尚未核实，不会重复生成: {exc}') from exc
        backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(done, ensure_ascii=False))
        return backend.get_task(task['id']) or task
    with backend.PLAN_EDIT_LOCK:
        if target_plan.is_file():
            document = json.loads(target_plan.read_text(encoding="utf-8-sig"))
            is_assembly = task["asset_id"] == payload.get("assembly_asset_id", backend.CURRENT_PROJECT.get("assembly_asset_id"))
            target = document.get("assembly", {}) if is_assembly else next((item for item in document.get("segments", []) if item.get("id") == task["asset_id"]), {})
            recorded_prompt = target.get("prompt_id" if is_assembly else "comfyui_task_id")
            previous_candidate = payload.get('previous_assembly_candidate')
            replaces_previous = (is_assembly and isinstance(previous_candidate, dict)
                                 and previous_candidate == {key: target.get(key) for key in ('prompt_id', 'output')})
            if recorded_prompt and recorded_prompt != prompt_id and not replaces_previous:
                raise HTTPException(409, "该片段已有其他生成版本；保留现有版本，请先人工核对历史结果")
            if not recorded_prompt and not replaces_previous and (target.get("output") if is_assembly else (target.get("video") or {}).get("path")):
                raise HTTPException(409, "该片段已有未关联回执的候选；保留现有候选，请先人工核对")
            version_recorded = (not is_assembly and any(
                isinstance(entry, dict) and entry.get("task_id") == task["id"]
                for entry in target.get("generation_history", [])
            ))
            if (not recorded_prompt or (replaces_previous and recorded_prompt != prompt_id)
                    or (not is_assembly and recorded_prompt == prompt_id and not version_recorded)):
                recorder = backend.record_assembly_result if is_assembly else backend.record_segment_result
                workflow = str(payload.get("workflow", ""))
                if is_assembly:
                    recorder(str(prompt_id), workflow, done, target_plan)
                else:
                    recorder(task["asset_id"], str(prompt_id), workflow, done, target_plan,
                             task_id=task["id"], payload=payload)
        backend.set_task(task["id"], status="succeeded", stop_requested=0, error=None, result=json.dumps(done, ensure_ascii=False))
    return backend.get_task(task["id"]) or task


@ROUTER.post("/api/tasks/{task_id}/resume")
@backend.gpu_admission
def resume(task_id: str) -> dict[str, Any]:
    task = backend.get_task(task_id)
    if task and task.get("project_id") != backend.CURRENT_PROJECT_ID:
        raise HTTPException(409, "该任务属于另一个项目，禁止在当前项目恢复")
    return backend.resume_saved_task(task)


@ROUTER.post("/api/projects/{project_id}/tasks/{task_id}/resume")
@backend.gpu_admission
def resume_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    task = backend.project_task_detail(project_id, task_id)
    manifest = backend.require_project_manifest(project_id)
    return backend.resume_saved_task(backend.task_in_project_context(task, manifest))


def task_in_project_context(task: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    project_id = str(manifest['id'])
    payload = dict(task['payload'])
    if payload.get('project_id', project_id) != project_id:
        raise HTTPException(409, '冻结任务与项目归属不一致，不能恢复')
    payload.setdefault('project_id', project_id)
    payload.setdefault('plan_path', str(backend.resolve_registered_path(str(manifest['plan_path']))))
    payload.setdefault('assembly_asset_id', manifest.get('assembly_asset_id', ''))
    return {**task, 'payload': payload}


def resume_saved_task(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task or task["status"] not in {"stopped", "failed", "needs_reconcile"}:
        raise HTTPException(409, "只有已停止、失败或待核对任务可以恢复")
    if task['payload'].get('kind') in {'speech', 'keyframe', 'audio_edit', 'audio_master', 'media_operation',
                                      'reference_decomposition', 'reference_semantic'}:
        return backend.reconcile_task(task)
    if task["status"] == "needs_reconcile" or task.get("prompt_id"):
        task = backend.reconcile_task(task)
        if task["status"] == "succeeded":
            return task
    backend.require_submission_capacity()
    payload = task["payload"]
    new_id = str(uuid.uuid4())
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id, task["asset_id"], "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), task["project_id"]))
        connection.commit()
    backend.queue_task(new_id)
    return backend.get_task(new_id) or {"id": new_id, "status": "preparing"}
