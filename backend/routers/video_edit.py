"""剪辑台：把已有视频按时间线剪开、排序、拼接并导出。

从 media-tasks 的执行模式照搬：写入 tasks 表 → 队列派发 → CPU 线程跑 ffmpeg →
产物登记进作品素材。所有源路径都先过 ``resolve_registered_path`` 白名单。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter, HTTPException

from .. import video_edit
from ..schemas import VideoEditProbeRequest, VideoEditRequest
from .. import app as backend

ROUTER = APIRouter()


def _resolve_source(value: str) -> Path:
    """任务产物在 ComfyUI output 下以「导演工作台工作目录/…」返回，不是登记路径。

    先走统一白名单；失败再把它当 output 目录内的相对路径试一次，仍然要求落在
    OUTPUT_ROOT 之内，不会因为这次放宽就读到工作区外的文件。
    """
    try:
        return backend.resolve_registered_path(value)
    except (ValueError, OSError):
        text = str(value).replace('\\', '/').lstrip('/')
        root = Path(backend.OUTPUT_ROOT).resolve()
        candidate = (root / text).resolve()
        if root in candidate.parents and candidate.is_file():
            return candidate
        raise


@ROUTER.get('/api/video-edit-capability')
def video_edit_capability() -> dict[str, Any]:
    return video_edit.capability()


@ROUTER.post('/api/video-edit-probe')
def video_edit_probe(request: VideoEditProbeRequest) -> dict[str, Any]:
    """给时间线读出每段时长与是否有原声；不读时长就没法画入点/出点。"""
    items: list[dict[str, Any]] = []
    for source in request.sources:
        try:
            path = _resolve_source(source)
            info = video_edit.probe(path)
        except (ValueError, OSError, video_edit.VideoEditFailed) as exc:
            items.append({'source': source, 'ok': False, 'error': str(exc)})
            continue
        items.append({'source': source, 'ok': True,
                      'duration': round(info['duration'], 3),
                      'width': info['width'], 'height': info['height'],
                      'fps': round(info['fps'], 3), 'has_audio': info['has_audio']})
    return {'items': items}


@ROUTER.get('/api/projects/{project_id}/video-sources')
def list_video_sources(project_id: str) -> dict[str, Any]:
    """作品 video 目录里现成的视频，供剪辑台直接当草料。

    素材列表只认「已登记的素材」和「成功任务的产物」。更早那批任务的记录被批量
    清理过，文件还在却列不出来；这个接口直接扫目录把这一块补上。
    """
    manifest = backend.require_project_manifest(project_id)
    try:
        workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f'作品目录不可读取：{exc}') from exc
    # 两处都要扫：作品自己的工作目录，以及 ComfyUI 输出目录里按 project_id 归档的
    # 产物目录。后者才是历次生成真正落盘的地方，任务记录被清掉以后只剩它还在。
    roots = [Path(workspace) / 'video']
    try:
        roots.append(Path(backend.OUTPUT_ROOT) / '导演工作台工作目录' / project_id / 'video')
    except Exception:
        pass
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.webm'}:
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            stat = path.stat()
            items.append({'path': str(path), 'title': path.name,
                          'size': stat.st_size, 'modified_at': stat.st_mtime})
    items.sort(key=lambda item: item['modified_at'], reverse=True)
    return {'roots': [str(root) for root in roots], 'items': items[:200]}


@ROUTER.post('/api/projects/{project_id}/video-edit-tasks', status_code=202)
def start_video_edit_task(project_id: str, request: VideoEditRequest) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    resolved: list[dict[str, Any]] = []
    for index, clip in enumerate(request.clips, 1):
        try:
            path = _resolve_source(clip.source)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, f'第 {index} 段素材不在允许读取的目录里：{exc}') from exc
        if not path.is_file():
            raise HTTPException(422, f'第 {index} 段素材不存在：{path.name}')
        if clip.end_seconds is not None and clip.end_seconds <= clip.start_seconds:
            raise HTTPException(422, f'第 {index} 段的出点必须大于入点')
        resolved.append({'path': str(path), 'label': clip.label or path.stem,
                         'start_seconds': clip.start_seconds, 'end_seconds': clip.end_seconds,
                         'volume': clip.volume, 'muted': clip.muted})
    background = None
    if request.background_music:
        try:
            music = _resolve_source(request.background_music)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, f'背景音乐不在允许读取的目录里：{exc}') from exc
        if not music.is_file():
            raise HTTPException(422, '背景音乐文件不存在')
        background = {'path': str(music), 'volume': request.background_volume}
    submission = {
        'kind': 'video_edit',
        'clips': [{'source': item['path'], 'start': item['start_seconds'],
                   'end': item['end_seconds'], 'volume': item['volume'], 'muted': item['muted']}
                  for item in resolved],
        'background_music': (background or {}).get('path'),
        'background_volume': request.background_volume,
        'crf': request.crf,
    }
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        if existing['payload']['submission_receipt']['request'] != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    ready = video_edit.capability()
    if not ready['available']:
        raise HTTPException(503, {'message': '剪辑引擎尚未就绪', **ready})
    backend.require_submission_capacity()
    task_id = str(uuid.uuid4())
    try:
        workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
        payload = video_edit.prepare(task_id, project_id, workspace, resolved,
                                     background_music=background, crf=request.crf)
        payload['execution_owner'] = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
    except video_edit.VideoEditFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, psutil.Error) as exc:
        raise HTTPException(422, f'剪辑任务准备失败：{exc}') from exc
    payload['project_id'] = project_id
    payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission}
    if backend.PRIVATE_WORKSPACES is not None:
        payload['owner_user'] = backend.PRIVATE_WORKSPACES.read()['projects'].get(project_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                           (task_id, f'edit-{task_id}', 'scheduler_waiting', now, now,
                            json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    backend.queue_task(task_id)
    return backend.get_task(task_id)


def complete_video_edit_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    task = backend.get_task(task['id']) or task
    if task.get('stop_requested') or task['status'] == 'stopped':
        backend.set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False),
                         error='已停止；完成的文件保留')
        return backend.get_task(task['id'])
    outputs = [dict(item) for item in receipt.get('outputs') or []]
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        for item in outputs:
            if item.get('kind') != 'video' or not item.get('path'):
                continue
            asset_id = task['asset_id']
            if any(asset.get('id') == asset_id for asset in assets):
                continue
            source = backend.root_relative_path(Path(item['path']))
            assets.append({'id': asset_id, 'kind': 'video',
                           'title': f"{item.get('title') or '剪辑成片'} · {time.strftime('%m-%d %H:%M')}",
                           'ready': True,
                           'subtitle': '剪辑台产物 · 待检查', 'origin': '工作台剪辑台',
                           'sources': {'A': source}, 'video_edit_task_id': task['id']})
            item['asset_id'] = asset_id
        manifest['assets'] = assets
        backend.save_project_manifest(manifest)
        if task['project_id'] == backend.CURRENT_PROJECT_ID:
            backend.apply_project_manifest(manifest)
    result = {**receipt, 'outputs': outputs}
    backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None,
                     result=json.dumps(result, ensure_ascii=False))
    return backend.get_task(task['id'])


def run_video_edit_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        backend.set_task(task_id, status='running')
        receipt = video_edit.execute(payload, lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
    except video_edit.VideoEditStopped as exc:
        backend.set_task(task_id, status='stopped', error=str(exc))
        return
    except (video_edit.VideoEditFailed, ValueError) as exc:
        backend.set_task(task_id, status='failed', error=f'剪辑导出失败：{exc}')
        return
    except Exception as exc:  # noqa: BLE001 - 执行回执不确定时不能当作失败
        backend.set_task(task_id, status='needs_reconcile', error=f'剪辑结果待核对：{exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        backend.set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        complete_video_edit_task(backend.get_task(task_id), receipt)
    except Exception as exc:  # noqa: BLE001
        backend.set_task(task_id, status='needs_reconcile', error=f'剪辑产物登记待核对：{exc}')
