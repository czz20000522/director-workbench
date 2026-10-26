"""ACE-Step 配乐任务：能力查询、显式参数提交与冻结工作流。

执行、监控与候选登记沿用工作台既有的 ComfyUI 提交链路；本模块只负责把用户显式
填写的配乐参数变成一张可复现的 api-graph。薄路由：域逻辑在 backend/music.py。
"""
from __future__ import annotations

import json
import time
from typing import Any
import uuid
from fastapi import APIRouter, HTTPException
from urllib.error import HTTPError, URLError

from .. import music
from ..schemas import MusicRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/music-capability')
def music_capability() -> dict[str, Any]:
    try:
        info = backend.request_json('/object_info')
        if not isinstance(info, dict):
            raise ValueError('节点注册表格式无效')
    except (OSError, HTTPError, URLError):
        return {'available': False, 'engine': 'ACE-Step 1.5 Turbo', 'id': 'ace-step-1.5', 'engine_offline': True,
                'missing_nodes': [], 'missing_models': [f'checkpoints/{music.CHECKPOINT}']
                if not music.CHECKPOINT_PATH.is_file() else [],
                'reason': '生成引擎待启动，点生成后会自动拉起。', 'note': music.capability({}).get('note', ''),
                'keyscales': list(music.KEYSCALES), 'timesignatures': list(music.TIME_SIGNATURES),
                'languages': list(music.LANGUAGES), 'mp3_qualities': list(music.MP3_QUALITIES),
                'duration_seconds': [music.MIN_DURATION, music.MAX_DURATION]}
    except ValueError as exc:
        raise HTTPException(503, f'无法检查 ComfyUI 配乐能力: {exc}') from exc
    return music.capability(info)


@ROUTER.post('/api/projects/{project_id}/music-tasks', status_code=202)
@backend.gpu_admission
def start_music_task(project_id: str, request: MusicRequest) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    submission = request.model_dump(mode='json')
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if (existing.get('payload') or {}).get('submission_receipt', {}).get('request') != submission:
            raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '请求标识已用于其他参数'})
        return existing
    try:
        frozen = music.validate({**submission, 'format': request.output_format})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        info = backend.request_json('/object_info')
    except (OSError, HTTPError, URLError):
        info = {}
    readiness = music.capability(info) if info else None
    if readiness is not None and not readiness['available']:
        raise HTTPException(503, {'message': '配乐环境尚未就绪', 'missing_models': readiness['missing_models'],
                                  'missing_nodes': readiness['missing_nodes']})
    backend.require_submission_capacity()
    task_id = str(uuid.uuid4())
    asset_id = f'music-{task_id}'
    prefix = f'导演工作台工作目录/{project_id}/music/{task_id}'
    graph = music.build_graph(frozen, prefix=prefix)
    workflow = backend.task_snapshot_root(project_id) / f'music-{task_id}.json'
    try:
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(json.dumps(graph, ensure_ascii=False), encoding='utf-8')
        payload = backend.freeze_task_payload({'kind': 'music', 'project_id': project_id, 'asset_id': asset_id,
                                               'pipeline_stage_id': 'music-generation', 'music': frozen,
                                               'workflow': str(workflow),
                                               'submission_receipt': {'key': request.idempotency_key, 'request': submission}},
                                              project_id=project_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f'无法冻结配乐参数: {exc}') from exc
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, asset_id, 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id) or {'id': task_id, 'status': 'preparing'}
