"""关键帧、媒体操作、音频编辑与语音任务：能力探测、启动与执行。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Literal
import uuid
from fastapi import APIRouter, HTTPException
import os
import psutil
from urllib.error import HTTPError, URLError
from .. import audio_edit_tasks, audio_master_tasks, keyframes, media_operations, output_resolution, speech
from ..schemas import AudioEditRequest, AudioMasterRequest, KeyframeRequest, MediaOperationRequest, SpeechRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/keyframe-capability')
def keyframe_capability(mode: Literal['reference', 'text'] = 'reference', creative_profile: Literal['general', 'adult-realism', 'adult-anime'] = 'general') -> dict[str, Any]:
    try:
        info = backend.request_json('/object_info')
        if not isinstance(info, dict):
            raise ValueError('节点注册表格式无效')
        return keyframes.capability(info, mode, creative_profile)
    except (OSError, ValueError, HTTPError, URLError) as exc:
        return {'available': False, 'engine': 'Qwen Image 2.1' if mode == 'text' else 'Qwen Image Edit 2511', 'requires_reference_image': mode != 'text', 'creative_profile': creative_profile,
                'missing_nodes': [], 'missing_models': [], 'reason': f'无法读取 ComfyUI 节点与模型: {exc}'}


def validate_creative_prompt(prompt: str, creative_profile: str) -> None:
    """Keep the adult entry scoped to fictional adults and non-explicit work."""
    if creative_profile == 'general':
        return
    lowered = prompt.casefold()
    blocked_groups = {
        '未成年人内容': ('未成年', '儿童', '小孩', '幼女', '幼男', '萝莉', 'loli', 'minor', 'child', 'teenager', 'teen'),
        '真人换脸或冒充真人': ('换脸', '换头', '真人脸', '深度伪造', 'deepfake', 'face swap', 'celebrity', '名人'),
        '露骨性行为或裸露私密部位': ('性交', '做爱', '性行为', '插入', '口交', '手淫', '射精', '裸体', '裸露', '私密部位', '隐私器官', '生殖器', '脱衣', '脱去衣物', '脱光', 'porn', 'explicit sex', 'intercourse', 'penetration', 'blowjob', 'masturbat', 'ejaculat', 'nude', 'naked', 'genital'),
    }
    for label, terms in blocked_groups.items():
        if any(term in lowered for term in terms):
            raise HTTPException(422, f'成人向创作区仅支持虚构成年人和非露骨内容，当前提示词包含{label}。')


def complete_keyframe_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    settings = task['payload'].get('output_size')
    if settings:
        processing_root = backend.OUTPUT_ROOT
        if backend.PRIVATE_WORKSPACES is not None and backend.PRIVATE_WORKSPACES.verify_task(task) is not None:
            processing_root = backend.OUTPUT_ROOT / '导演工作台工作目录' / task['project_id']
        path, profile = output_resolution.resize(receipt['output'], processing_root,
            settings['resolution'], settings['aspect_ratio'],
            lambda: bool((backend.get_task(task['id']) or {}).get('stop_requested')))
        receipt = {**receipt, 'output': str(path), 'native_output': receipt['output'],
                   'width': profile['width'], 'height': profile['height'], 'output_size': profile}
    if backend.PRIVATE_WORKSPACES is not None:
        published = backend.PRIVATE_WORKSPACES.publish(task, Path(receipt['output']), backend.OUTPUT_ROOT)
        if receipt.get('native_output'):
            receipt = {**receipt, 'published_native_output': str(backend.PRIVATE_WORKSPACES.publish(
                task, Path(receipt['native_output']), backend.OUTPUT_ROOT, 1))}
        receipt = {**receipt, 'engine_output': receipt['output'], 'output': str(published)}
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        asset_id = f"keyframe-{task['id']}"
        if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
            source = backend.root_relative_path(Path(receipt['output']))
            assets.append({'id': asset_id, 'kind': 'image', 'title': f"关键帧 · {task['payload']['request']['prompt'][:32]}",
                           'ready': True, 'subtitle': '关键帧候选 · 待审核', 'origin': task['payload']['request'].get('engine', 'Qwen Image Edit 2511'),
                           'sources': {'A': source}, 'image_path': source, 'keyframe_task_id': task['id']})
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if task['project_id'] == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
    backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(receipt, ensure_ascii=False))
    return backend.get_task(task['id']) or task


@ROUTER.post('/api/projects/{project_id}/keyframe-tasks', status_code=202)
@backend.gpu_admission
def start_keyframe_task(project_id: str, request: KeyframeRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    backend.validate_creative_prompt(request.prompt, request.creative_profile)
    submission = {'kind': 'keyframe', 'prompt': request.prompt, 'reference_image': request.reference_image, 'seed': request.seed,
                  'creative_profile': request.creative_profile}
    if request.output_resolution != 'native':
        if request.mode != 'text':
            raise HTTPException(422, '参考图模式暂时沿用原生尺寸')
        submission['output_resolution'] = request.output_resolution
    if request.mode == 'text':
        if request.reference_image is not None:
            raise HTTPException(422, '文字生图不接收参考图片，请选择参考图模式')
        submission.update(mode='text', width=request.width, height=request.height)
    elif not request.reference_image:
        raise HTTPException(422, '参考图模式需要选择图片')
    elif (request.width, request.height) != (768, 1344):
        raise HTTPException(422, '参考图模式的画幅由参考图片决定')
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if existing['payload']['submission_receipt']['request'] != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    try:
        output_resolution.require_available(request.output_resolution)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    backend.require_submission_capacity()
    readiness = backend.keyframe_capability(mode='text') if request.mode == 'text' else backend.keyframe_capability()
    if not readiness['available']:
        raise HTTPException(503, {'message': '关键帧生成环境尚未就绪', **readiness})
    task_id = str(uuid.uuid4())
    try:
        workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
        prefix = f'导演工作台工作目录/{project_id}/关键帧/{task_id}'
        if request.mode == 'text':
            payload = keyframes.prepare_text(task_id, request.prompt, workspace / 'keyframe-tasks', prefix, request.seed, request.width, request.height, request.creative_profile)
            if request.output_resolution != 'native':
                payload['output_size'] = {'resolution': request.output_resolution,
                    'aspect_ratio': '1:1' if request.width == request.height else '16:9' if request.width > request.height else '9:16'}
                payload['request']['output_resolution'] = request.output_resolution
        else:
            reference = backend.resolve_registered_path(request.reference_image)
            payload = keyframes.prepare(task_id, request.prompt, reference, workspace / 'keyframe-tasks', prefix, request.seed, request.creative_profile)
        payload = backend.freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    payload.update(project_id=project_id, asset_id=f'keyframe-{task_id}',
                   submission_receipt={'key': request.idempotency_key, 'request': submission})
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, payload['asset_id'], 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id)


@ROUTER.get('/api/speech-capability')
def speech_capability() -> dict[str, Any]:
    return speech.capability()


@ROUTER.get('/api/media-capability')
def media_capability() -> dict[str, Any]:
    return media_operations.capability()


def media_operation_source(manifest: dict[str, Any], asset_id: str, operation: str) -> tuple[Path, dict[str, Any]]:
    """Select only a current, registered project asset; callers cannot submit paths."""
    asset = next((a for a in manifest.get('assets', []) if isinstance(a, dict) and a.get('id') == asset_id), None)
    value = (asset.get('sources') or {}).get('A') if asset else None
    kind = asset.get('kind') if asset else None
    if not asset:
        plan, _ = backend.load_project_plan(str(manifest['id']))
        segment = next((s for s in plan['segments'] if isinstance(s, dict) and s.get('id') == asset_id), None)
        if segment:
            value, kind = (segment.get('video') or {}).get('path'), 'video'
        elif asset_id == manifest.get('assembly_asset_id'):
            value, kind = (plan.get('assembly') or {}).get('output'), 'video'
    allowed = {'audio', 'video'} if operation == 'transcribe' else {'video'}
    if kind not in allowed or not isinstance(value, str) or not value:
        raise HTTPException(422, '请选择当前作品已有结果的音视频素材')
    try:
        source = backend.resolve_registered_path(value)
        if not source.is_file():
            raise ValueError('源素材不存在')
        info = source.stat()
        if info.st_size > 2 * 1024**3:
            raise ValueError('首版媒体处理限制单个源文件不超过 2 GiB')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return source, {'asset_id': asset_id, 'kind': kind, 'source': value,
                    'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


@ROUTER.post('/api/projects/{project_id}/media-tasks', status_code=202)
def start_media_operation(project_id: str, request: MediaOperationRequest) -> dict[str, Any]:
    with backend.MEDIA_OPERATION_LOCK:
        manifest = backend.require_project_manifest(project_id)
        submission = {'kind': 'media_operation', 'operation': request.operation,
                      'source_asset_id': request.source_asset_id, 'parameters': request.parameters()}
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
        backend.require_submission_capacity()
        ready = media_operations.capability().get('operations', {}).get(request.operation, {})
        if not ready.get('available'):
            raise HTTPException(503, {'message': '媒体处理环境尚未就绪', **ready})
        source, source_record = (None, None) if request.operation == 'silence' else backend.media_operation_source(manifest, request.source_asset_id, request.operation)
        task_id = str(uuid.uuid4())
        try:
            workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
            payload = media_operations.prepare(task_id, request.operation, source, workspace / 'media-tasks', parameters=request.parameters())
            owner = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        except (OSError, ValueError, psutil.Error, media_operations.MediaOperationFailed) as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update(project_id=project_id, source_asset=source_record, execution_owner=owner,
                       submission_receipt={'key': request.idempotency_key, 'request': submission})
        if backend.PRIVATE_WORKSPACES is not None:
            payload['owner_user'] = backend.PRIVATE_WORKSPACES.read()['projects'].get(project_id)
        now = time.time()
        with backend.DB_LOCK, backend.db() as connection:
            backend.check_queue_limits(connection, backend.settings_owner())
            connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                (task_id, f'media-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        backend.queue_task(task_id)
        return backend.get_task(task_id)


def complete_media_operation_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    with backend.MEDIA_OPERATION_LOCK:
        task = backend.get_task(task['id']) or task
        if task.get('stop_requested') or task['status'] == 'stopped':
            backend.set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False), error='已停止；完成的文件保留，不登记为候选')
            return backend.get_task(task['id'])
        outputs = []
        with backend.MATERIAL_REUSE_LOCK:
            manifest = backend.require_project_manifest(task['project_id'])
            assets = list(manifest.get('assets') or [])
            first = True
            for index, output in enumerate(receipt['outputs']):
                output = dict(output)
                diagnostic = task['payload']['operation'] == 'video_qc'
                if diagnostic:
                    output['purpose'] = 'diagnostic'
                if not diagnostic and output['kind'] in {'audio', 'video', 'image'}:
                    asset_id = task['asset_id'] if first else f"{task['asset_id']}-{index:02d}"
                    first = False
                    output['asset_id'] = asset_id
                    if not any(a.get('id') == asset_id for a in assets):
                        source = backend.root_relative_path(Path(output['path']))
                        asset = {'id': asset_id, 'kind': output['kind'], 'title': output.get('title') or '媒体处理候选',
                                 'ready': True, 'subtitle': '媒体工具产物 · 待检查', 'origin': '工作台媒体工具',
                                 'sources': {'A': source}, 'media_task_id': task['id'],
                                 'operation': task['payload']['operation'], 'source_asset': task['payload'].get('source_asset')}
                        if output['kind'] == 'image':
                            asset['image_path'] = source
                        assets.append(asset)
                outputs.append(output)
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if task['project_id'] == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
        result = {**receipt, 'outputs': outputs}
        backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(result, ensure_ascii=False))
        return backend.get_task(task['id'])


def run_media_operation_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        backend.set_task(task_id, status='running')
        receipt = media_operations.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
    except media_operations.MediaOperationStopped as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
        return
    except (media_operations.MediaOperationFailed, ValueError) as exc:
        backend.set_task(task_id, status='failed', error=f'媒体处理失败: {exc}')
        return
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'媒体处理结果待核对: {exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        backend.set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        backend.complete_media_operation_task(backend.get_task(task_id), receipt)
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'媒体工具产物登记待核对: {exc}')


def audio_edit_owner_alive(payload: dict[str, Any]) -> bool:
    if payload.get('execution_finished_at'):
        return False  # Written only after the synchronous CPU executor has returned.
    owner = payload.get('execution_owner') or {}
    if type(owner.get('pid')) is not int or type(owner.get('created_at')) not in (int, float):
        raise HTTPException(409, '缺少CPU执行进程身份，不能确认执行结束')
    try:
        return psutil.Process(owner['pid']).create_time() == owner['created_at']
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError) as exc:
        raise HTTPException(409, '无法核对CPU执行进程身份，保留待核对状态') from exc


def audio_master_owner_alive(payload: dict[str, Any]) -> bool:
    return backend.audio_edit_owner_alive(payload)


def audio_edit_source(manifest: dict[str, Any], asset_id: str) -> tuple[Path, dict[str, Any]]:
    asset = next((item for item in manifest.get('assets', []) if isinstance(item, dict) and item.get('id') == asset_id and item.get('kind') == 'audio'), None)
    value = (asset.get('sources') or {}).get('A') if asset else None
    if not isinstance(value, str) or not value:
        raise HTTPException(422, '请选择当前作品已登记且有A来源的音频素材')
    try:
        path = backend.resolve_registered_path(value)
        if not path.is_file():
            raise ValueError('已登记音频文件不存在')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return path, {'asset_id': asset_id, 'source': value, 'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}


@ROUTER.post('/api/projects/{project_id}/audio-edit-tasks', status_code=202)
def start_audio_edit_task(project_id: str, request: AudioEditRequest) -> dict[str, Any]:
    with backend.AUDIO_EDIT_LOCK:
        manifest = backend.require_project_manifest(project_id)
        submission = {'kind': 'audio_edit', **request.model_dump(exclude={'idempotency_key'})}
        if not request.pad_silence:
            submission.pop('pad_silence', None)  # Preserve old default-request idempotency receipts.
        if not request.idempotency_key.strip():
            raise HTTPException(422, '请求标识不能为空')
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
        backend.require_submission_capacity()
        source, source_record = backend.audio_edit_source(manifest, request.source_asset_id)
        background, background_record = (None, None) if request.background_asset_id is None else backend.audio_edit_source(manifest, request.background_asset_id)
        task_id = str(uuid.uuid4())
        try:
            workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
            payload = audio_edit_tasks.prepare(task_id, source, workspace / 'audio-edit-tasks',
                parameters={key: submission.get(key, default) for key, default in audio_edit_tasks.DEFAULTS.items()}, background=background)
            owner = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        except (OSError, ValueError, psutil.Error) as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update(project_id=project_id, source_asset=source_record, background_asset=background_record,
                       execution_owner=owner, submission_receipt={'key': request.idempotency_key, 'request': submission})
        if backend.PRIVATE_WORKSPACES is not None:
            payload['owner_user'] = backend.PRIVATE_WORKSPACES.read()['projects'].get(project_id)
        now = time.time()
        with backend.DB_LOCK, backend.db() as connection:
            backend.check_queue_limits(connection, backend.settings_owner())
            connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                (task_id, f'audio-edit-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        backend.queue_task(task_id)
        return backend.get_task(task_id)


def complete_audio_edit_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    with backend.AUDIO_EDIT_LOCK:
        task = backend.get_task(task['id']) or task
        if task.get('stop_requested') or task['status'] == 'stopped':
            backend.set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False), error='CPU处理已结束；已按停止请求保留结果但不登记候选')
            return backend.get_task(task['id'])
        with backend.MATERIAL_REUSE_LOCK:
            manifest = backend.require_project_manifest(task['project_id'])
            assets = list(manifest.get('assets') or [])
            asset_id = f"audio-edit-{task['id']}"
            if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
                assets.append({'id': asset_id, 'kind': 'audio', 'title': '编辑音频', 'ready': True,
                    'subtitle': '编辑音频候选 · 待试听', 'origin': 'CPU音频编辑',
                    'sources': {'A': backend.root_relative_path(Path(receipt['output']))}, 'start': 0, 'end': receipt['duration_seconds'],
                    'audio_edit_task_id': task['id'], 'source_asset': task['payload']['source_asset'],
                    'background_asset': task['payload'].get('background_asset'), 'edit_parameters': task['payload']['request']['parameters']})
                manifest['assets'] = assets
                backend.save_project_manifest(manifest)
                if task['project_id'] == backend.CURRENT_PROJECT_ID:
                    backend.apply_project_manifest(manifest)
        backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(receipt, ensure_ascii=False))
        return backend.get_task(task['id'])


def run_audio_edit_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        receipt = audio_edit_tasks.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
    except audio_edit_tasks.AudioEditStoppedBeforeStart as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
        return
    except ValueError as exc:
        backend.set_task(task_id, status='failed', error=f'CPU音频编辑校验失败: {exc}')
        return
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'CPU音频编辑结果待核对: {exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        backend.set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        backend.complete_audio_edit_task(backend.get_task(task_id), receipt)
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'CPU音频候选登记待核对: {exc}')


@ROUTER.post('/api/projects/{project_id}/audio-master-tasks', status_code=202)
def start_audio_master_task(project_id: str, request: AudioMasterRequest) -> dict[str, Any]:
    """Create a long complete-master candidate from project-owned audio assets."""
    with backend.AUDIO_EDIT_LOCK:
        manifest = backend.require_project_manifest(project_id)
        submission = {'kind': 'audio_master', **request.model_dump(exclude={'idempotency_key'})}
        if not request.idempotency_key.strip():
            raise HTTPException(422, '请求标识不能为空')
        existing = backend.find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
        backend.require_submission_capacity()
        source, source_record = backend.audio_edit_source(manifest, request.source_asset_id)
        background, background_record = (None, None) if request.background_asset_id is None else backend.audio_edit_source(manifest, request.background_asset_id)
        task_id = str(uuid.uuid4())
        try:
            workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
            payload = audio_master_tasks.prepare(task_id, source, workspace / 'audio-master-tasks',
                parameters={key: submission.get(key, default) for key, default in audio_master_tasks.DEFAULTS.items()}, background=background)
            owner = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        except (OSError, ValueError, psutil.Error) as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update(project_id=project_id, source_asset=source_record, background_asset=background_record,
                       execution_owner=owner, submission_receipt={'key': request.idempotency_key, 'request': submission})
        if backend.PRIVATE_WORKSPACES is not None:
            payload['owner_user'] = backend.PRIVATE_WORKSPACES.read()['projects'].get(project_id)
        now = time.time()
        with backend.DB_LOCK, backend.db() as connection:
            backend.check_queue_limits(connection, backend.settings_owner())
            connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                (task_id, f'audio-master-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        backend.queue_task(task_id)
        return backend.get_task(task_id)


def complete_audio_master_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    with backend.AUDIO_EDIT_LOCK:
        task = backend.get_task(task['id']) or task
        if task.get('stop_requested') or task['status'] == 'stopped':
            backend.set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False), error='CPU处理已结束；已按停止请求保留结果但不登记候选')
            return backend.get_task(task['id'])
        asset_id = f'audio-master-{task["id"]}'
        with backend.MATERIAL_REUSE_LOCK:
            manifest = backend.require_project_manifest(task['project_id'])
            assets = list(manifest.get('assets') or [])
            if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
                assets.append({'id': asset_id, 'kind': 'audio', 'title': '全片母版后期候选', 'ready': True,
                    'subtitle': '全片母版后期 · 待试听', 'origin': 'CPU全片音频后期',
                    'sources': {'A': backend.root_relative_path(Path(receipt['output']))}, 'start': 0,
                    'end': receipt['duration_seconds'], 'audio_master_task_id': task['id'],
                    'source_asset': task['payload']['source_asset'], 'background_asset': task['payload'].get('background_asset'),
                    'master_report': receipt.get('report'), 'edit_parameters': task['payload']['request']['parameters']})
                manifest['assets'] = assets
                backend.save_project_manifest(manifest)
                if task['project_id'] == backend.CURRENT_PROJECT_ID:
                    backend.apply_project_manifest(manifest)
        result = {**receipt, 'candidate_asset_id': asset_id}
        backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(result, ensure_ascii=False))
        return backend.get_task(task['id'])


def run_audio_master_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        receipt = audio_master_tasks.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
    except audio_master_tasks.AudioMasterStoppedBeforeStart as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
        return
    except ValueError as exc:
        backend.set_task(task_id, status='failed', error=f'CPU全片音频后期校验失败: {exc}')
        return
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'CPU全片音频后期结果待核对: {exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        backend.set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        backend.complete_audio_master_task(backend.get_task(task_id), receipt)
    except Exception as exc:
        backend.set_task(task_id, status='needs_reconcile', error=f'CPU全片音频后期候选登记待核对: {exc}')


def complete_speech_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    # Keep the candidate independent: do not overwrite a shot's selected audio.
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        asset_id = f"speech-{task['id']}"
        if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
            assets.append({'id': asset_id, 'kind': 'audio', 'title': f"台词 · {task['payload']['request']['text'][:32]}",
                           'ready': True, 'subtitle': '声音候选 · 待试听',
                           'origin': 'AuK-Flash' if task['payload'].get('engine') == 'auk-flash' else 'IndexTTS 2.5',
                           'sources': {'A': backend.root_relative_path(Path(receipt['output']))},
                           'start': 0, 'end': receipt['duration_seconds'],
                           'speech_task_id': task['id']})
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if task['project_id'] == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
    backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None,
             result=json.dumps(receipt, ensure_ascii=False))
    return backend.get_task(task['id']) or task


def run_speech_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        receipt = speech.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
        backend.complete_speech_task(backend.get_task(task_id), receipt)
    except speech.SpeechStopped as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
    except speech.SpeechFailed as exc:
        backend.set_task(task_id, status='failed', error=str(exc))
    except Exception as exc:
        # A process/receipt error must never enable an automatic duplicate render.
        backend.set_task(task_id, status='needs_reconcile', error=str(exc))


@ROUTER.post('/api/projects/{project_id}/speech-tasks', status_code=202)
@backend.gpu_admission
def start_speech_task(project_id: str, request: SpeechRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    submission = {'kind': 'speech', 'text': request.text}
    if request.mode == 'design':
        submission.update(mode='design', voice_description=request.voice_description)
    else:
        submission['voice_reference'] = request.voice_reference
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        previous = existing['payload']['submission_receipt']['request']
        if 'engine' in previous:
            submission.update(engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed)
        elif request.model_fields_set & {'gen_seconds', 'seed'}:
            raise HTTPException(409, '旧声音请求不能追加新模型参数，请查询原任务')
        if previous != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    submission.update(engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed)
    readiness = speech.capability()
    if not readiness['available']:
        raise HTTPException(503, {'message': '声音生成环境尚未就绪', 'missing': readiness['missing']})
    backend.require_submission_capacity()
    try:
        reference = backend.resolve_registered_path(request.voice_reference) if request.mode == 'reference' else None
        workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
        task_id = str(uuid.uuid4())
        payload = speech.prepare(task_id, request.text, reference, workspace / 'speech-tasks',
                                 engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed,
                                 mode=request.mode, voice_description=request.voice_description)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    payload.update(project_id=project_id, submission_receipt={'key': request.idempotency_key, 'request': submission})
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, f'speech-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id)
