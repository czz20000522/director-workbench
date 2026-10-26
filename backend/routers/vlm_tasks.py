"""画面观察任务：能力探测、启动、执行与核对。

只读域：模型回答保存在任务回执里，不登记创作素材、不写入分镜、不自动采用。
沿用 backend/app.py 的既有做法：本模块只在调用时通过 ``backend.<名称>`` 读取工作台
对象，因此 app.py 上的 monkeypatch 语义完全不变。
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import uuid
from fastapi import APIRouter, HTTPException

from .. import vlm
from ..schemas import VlmObservationRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/vlm-capability')
def vlm_capability() -> dict[str, Any]:
    return vlm.capability()


def complete_vlm_observation_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    task = backend.get_task(task['id']) or task
    if task.get('stop_requested') or task['status'] == 'stopped':
        backend.set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False),
                         error='已停止；观察结果不登记')
        return backend.get_task(task['id'])
    # An observation is evidence about one frame, never a creative asset: it stays
    # in the task receipt so nothing downstream can treat it as an adopted output.
    backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None,
                     result=json.dumps(receipt, ensure_ascii=False))
    return backend.get_task(task['id']) or task


def run_vlm_observation_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        backend.set_task(task_id, status='running')
        receipt = vlm.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
    except vlm.VlmStopped as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
        return
    except (vlm.VlmFailed, ValueError) as exc:
        backend.set_task(task_id, status='failed', error=f'画面观察失败: {exc}')
        return
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'画面观察结果待核对: {exc}')
        return
    finally:
        backend.set_task(task_id, payload=json.dumps({**payload, 'execution_finished_at': time.time()}, ensure_ascii=False))
    try:
        backend.complete_vlm_observation_task(backend.get_task(task_id), receipt)
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'画面观察回执待核对: {exc}')


@ROUTER.post('/api/projects/{project_id}/vlm-tasks', status_code=202)
@backend.gpu_admission
def start_vlm_observation(project_id: str, request: VlmObservationRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    submission = {'kind': 'vlm_observation', 'provider': request.provider, 'source': request.source,
                  'frame_seconds': request.frame_seconds,
                  'prompt': request.prompt.strip(), 'temperature': request.temperature,
                  'num_predict': request.num_predict}
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if existing['payload']['submission_receipt']['request'] != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    readiness = vlm.capability()
    provider_state = (readiness.get('providers') or {}).get(request.provider) or {}
    if not provider_state.get('available'):
        raise HTTPException(503, {'message': f'{provider_state.get("label") or request.provider} 当前不可用',
                                  'missing': provider_state.get('missing') or readiness['missing'],
                                  'reason': provider_state.get('reason', '') or readiness.get('reason', '')})
    backend.require_submission_capacity()
    try:
        source = backend.resolve_registered_path(request.source)
        workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
        task_id = str(uuid.uuid4())
        payload = vlm.prepare(task_id, source, workspace / 'vlm-tasks', prompt=request.prompt,
                              frame_seconds=request.frame_seconds, temperature=request.temperature,
                              num_predict=request.num_predict, provider=request.provider)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    payload.update(project_id=project_id, model=payload['request']['model'],
                   submission_receipt={'key': request.idempotency_key, 'request': submission})
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, f'vlm-{task_id}', 'scheduler_waiting', now, now,
                            json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id)
