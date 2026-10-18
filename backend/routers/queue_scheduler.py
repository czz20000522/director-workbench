"""队列与调度执行：队列上限、入队校验、调度资源判定与执行。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from .. import background_jobs, task_scheduler
from .. import app as backend


ROUTER = APIRouter()


def check_queue_limits(connection, owner, count=1):
    try:
        backend.TASK_SCHEDULER.check_limits(connection, owner, count)
    except task_scheduler.QueueLimitError as exc:
        raise HTTPException(429, exc.detail) from exc


def queue_task(task_id: str) -> None:
    backend.TASK_SCHEDULER.wake()


def ensure_asset_not_pending(project_id: str, asset_id: str) -> None:
    statuses = tuple(task_scheduler.WAITING | task_scheduler.EXECUTING)
    with backend.DB_LOCK, backend.db() as connection:
        row = connection.execute(f"SELECT id FROM tasks WHERE project_id=? AND asset_id=? AND status IN ({','.join('?' for _ in statuses)})",
                                 (project_id, asset_id, *statuses)).fetchone()
    if row:
        raise HTTPException(409, {'code': 'asset_task_pending', 'message': '此分镜已有等待、执行或待核对任务，请查看原回执', 'task_id': row['id'], 'retryable': False})


def scheduler_resource_reason():
    if backend.REFERENCE_DECOMPOSE_ACTIVE or backend.REVERSE_SEMANTIC_LOCK.locked():
        return {'code': 'reference_running', 'message': '等待参考分析释放共享资源'}
    with background_jobs.LOCK:
        jobs = background_jobs.records(backend.analysis_job_root())
    if any(job['status'] in background_jobs.ACTIVE and not job.get('task_id') for job in jobs):
        return {'code': 'analysis_needs_reconcile', 'message': '等待旧参考分析结束或核对回执'}
    queue = backend.request_json('/queue')
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise ValueError('ComfyUI queue unavailable')
    if queue['queue_running'] or queue['queue_pending']:
        return {'code': 'external_queue_busy', 'message': '等待 ComfyUI 当前任务结束，不抢占外部任务'}
    return None


def revalidate_queued_task(task):
    payload = task['payload']
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    manifest = backend.load_project_manifest(task['project_id'])
    if payload.get('workflow') and not backend.resolve_registered_path(payload['workflow']).is_file():
        raise ValueError('冻结工作流无法读取，请重新预检')
    for material in (payload.get('execution_snapshot') or {}).get('staged_materials', []):
        path = Path(material['path'])
        if not path.is_file() or path.stat().st_size != material['size_bytes']:
            raise ValueError('冻结素材缺失或内容大小变化，请重新预检')
    for key in ('source_asset', 'background_asset'):
        record = payload.get(key)
        if record:
            path = backend.resolve_registered_path(record['source'])
            if not path.is_file() or path.stat().st_size != record['size'] or path.stat().st_mtime_ns != record['mtime_ns']:
                raise ValueError('冻结素材已改变，请重新登记输入')
    if payload.get('kind') in {'reference_semantic', 'reference_decomposition'}:
        key = 'analysis_path' if payload['kind'] == 'reference_semantic' else 'source'
        if not backend.resolve_registered_path(payload[key]).is_file():
            raise ValueError('参考分析输入无法读取')
    if payload.get('segment_dependencies'):
        document = json.loads(backend.resolve_registered_path(manifest['plan_path']).read_text(encoding='utf-8-sig'))
        blockers = backend.segment_dependency_blockers(document, {'dependencies': payload['segment_dependencies']})
        if blockers:
            return blockers[0]
    return None


def execute_scheduled_task(task):
    payload = task['payload']
    kind = payload.get('kind')
    if kind == 'speech':
        return backend.run_speech_task(task['id'], payload)
    if kind == 'audio_edit':
        return backend.run_audio_edit_task(task['id'], payload)
    if kind == 'media_operation':
        return backend.run_media_operation_task(task['id'], payload)
    if kind == 'reference_semantic':
        job = dict(payload)
        if not backend.REVERSE_SEMANTIC_LOCK.acquire(blocking=False):
            raise RuntimeError('Reference execution slot changed')
        backend.set_task(task['id'], status='running')
        background_jobs.update(backend.analysis_job_root(), job, status='running', owner=background_jobs.SESSION)
        try:
            backend.execute_reverse_semantic(task['project_id'], payload['analysis_id'], Path(payload['analysis_path']), job)
            status = 'stopped' if (backend.get_task(task['id']) or {}).get('stop_requested') else 'succeeded'
            backend.set_task(task['id'], status=status)
            background_jobs.update(backend.analysis_job_root(), job, status=status, owner=background_jobs.SESSION)
        except backend.ReferenceStopped as exc:
            backend.set_task(task['id'], status='stopped', error=str(exc))
            background_jobs.update(backend.analysis_job_root(), job, status='stopped', error=str(exc), owner=background_jobs.SESSION)
        except HTTPException as exc:
            backend.set_task(task['id'], status='failed', error=str(exc.detail))
            background_jobs.update(backend.analysis_job_root(), job, status='failed', error=str(exc.detail), owner=background_jobs.SESSION)
        except Exception as exc:
            backend.set_task(task['id'], status='needs_reconcile', error=str(exc))
            background_jobs.update(backend.analysis_job_root(), job, status='needs_reconcile', error=str(exc))
        return
    if kind == 'reference_decomposition':
        backend.set_task(task['id'], status='running')
        try:
            backend.run_tracked_reference_decomposition(payload['command'], task_id=task['id'])
        except backend.ReferenceStopped as exc:
            backend.set_task(task['id'], status='stopped', error=str(exc))
            return
        except HTTPException as exc:
            backend.set_task(task['id'], status='failed', error=str(exc.detail))
            return
        document, _ = backend.read_reverse_analysis(task['project_id'], payload['analysis_id'])
        status = 'stopped' if (backend.get_task(task['id']) or {}).get('stop_requested') else 'succeeded'
        backend.set_task(task['id'], status=status, result=json.dumps(document, ensure_ascii=False))
        return
    runner = backend.run_workflow_submission if payload.get('workflow') else backend.run_submission
    runner(task['id'], payload)
