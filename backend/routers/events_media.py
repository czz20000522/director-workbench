"""事件流与媒体文件：状态/任务 SSE 事件流、素材与工作流文件下载。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
import wave
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import app as backend
from ..secure_files import VerifiedFileResponse, checked_file


ROUTER = APIRouter()


@ROUTER.get("/api/projects/{project_id}/events")
async def project_events(project_id: str) -> StreamingResponse:
    backend.require_project_manifest(project_id)
    async def stream():
        while True:
            status_data, task_data = await asyncio.gather(asyncio.to_thread(backend.status), asyncio.to_thread(backend.project_task_list, project_id, 100, 0))
            yield f"data: {json.dumps({'status': status_data, 'tasks': task_data['tasks'], 'project_id': project_id}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@ROUTER.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        while True:
            status_data, task_data = await asyncio.gather(asyncio.to_thread(backend.status), asyncio.to_thread(backend.tasks))
            yield f"data: {json.dumps({'status': status_data, 'tasks': task_data}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@ROUTER.get("/media/{asset_name}")
def media(asset_name: str) -> FileResponse:
    path = backend.MEDIA.get(asset_name)
    if path is None:
        raise HTTPException(404, "素材不存在")
    try:
        candidate, identity = checked_file(path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, "素材不存在") from exc
    return VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())


@ROUTER.get("/media-file")
def media_file(path: str) -> FileResponse:
    """Serve an authorized asset from configured storage roots."""
    candidate = None
    try:
        candidate = backend.resolve_registered_path(path)
    except ValueError:
        # 相对路径先按登记规则解析；解析不出来时再按输出根试一次。
        candidate = None
    if candidate is None or not candidate.is_file():
        # 历史结果里同时存在绝对路径和相对输出路径（「导演工作台工作目录/…」）。
        # 相对路径按 SHARED_ROOT 解析会落到错误位置，结果就是图片地址 404、页面上留一块空白。
        text = str(path).replace('\\', '/').lstrip('/')
        root = Path(backend.OUTPUT_ROOT).resolve()
        attempt = (root / text).resolve()
        if root in attempt.parents and attempt.is_file():
            candidate = attempt
    if candidate is None:
        raise HTTPException(403, '素材路径不在允许的目录里')
    try:
        candidate, identity = checked_file(candidate)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, "素材不存在") from exc
    return VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())


@ROUTER.get("/workflow-file")
def workflow_file(path: str) -> FileResponse:
    """Download a project-owned ComfyUI JSON graph for browser review/import."""
    raw = str(path).replace("\\", "/").strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        prefix = "ComfyUI-Workspace/projects/director-workbench/"
        relative = raw[len(prefix):] if raw.startswith(prefix) else raw
        candidate = backend.PROJECT / relative
    candidate = backend.registered_candidate(candidate)
    if backend.PRIVATE_WORKSPACES is not None and backend.current_principal.get() is not None:
        candidate = backend.PRIVATE_WORKSPACES.file(candidate)
        if candidate.suffix.lower() != '.json':
            raise HTTPException(415, '这里只允许下载 JSON 工作流')
        try:
            candidate, identity = checked_file(candidate)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise HTTPException(404, '工作流不存在')
        return VerifiedFileResponse(candidate, identity=identity, media_type='application/json', filename=candidate.name, stat_result=candidate.stat())
    try:
        candidate = candidate.resolve()
        workflow_roots = [backend.PROJECT.resolve(), backend.DIRECTOR_WORKSPACES_ROOT.resolve()]
        workflow_roots.extend(destination for source, destination in backend.legacy_mappings(backend.ROOT) if source == backend.COMFY_ROOT / 'ComfyUI-Workspace/projects/director-workbench/workspaces')
        if not any(candidate.is_relative_to(root) for root in workflow_roots):
            raise ValueError('Workflow outside configured project roots')
    except ValueError as exc:
        raise HTTPException(403, "工作流路径不在导演台项目目录内") from exc
    if candidate.suffix.lower() != ".json":
        raise HTTPException(415, "这里只允许下载 JSON 工作流")
    try:
        candidate, identity = checked_file(candidate)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, "工作流不存在")
    return VerifiedFileResponse(candidate, identity=identity, media_type="application/json", filename=candidate.name, stat_result=candidate.stat())


class DialogueAudioRequest(BaseModel):
    """截取一段视频的音轨，给「按台词配音」当音色参考。"""

    model_config = ConfigDict(extra='forbid')

    source: str = Field(min_length=1, max_length=1000)
    # 参考音轨只用来认音色：太长会让配音引擎超时（参考 + 待生成合计上限 30 秒）。
    seconds: float = Field(default=10.0, gt=0, le=20)


@ROUTER.post('/api/projects/{project_id}/dialogue/audio')
def extract_dialogue_audio(project_id: str, request: DialogueAudioRequest) -> dict:
    """把上一条视频里的一小段人声抽成 WAV，作为下一段台词的音色参考。

    只读、只写项目工作区里的 dialogue/ 目录；不碰原视频。
    """
    manifest = backend.require_project_manifest(project_id)
    try:
        source = backend.resolve_registered_path(request.source)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not source.is_file():
        raise HTTPException(404, '作为音色参考的视频不存在')
    workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
    folder = workspace / 'dialogue'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{uuid.uuid4().hex}.wav'
    command = [str(backend.media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y',
               '-i', str(source), '-t', f'{request.seconds:.2f}', '-vn',
               '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(target)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=90,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(422, f'读取这段视频的音轨失败：{exc}') from exc
    if not target.is_file() or target.stat().st_size < 2048:
        target.unlink(missing_ok=True)
        raise HTTPException(422, '这条视频没有可用音轨（可能生成时没带声音）')
    try:
        with wave.open(str(target), 'rb') as handle:
            seconds = handle.getnframes() / float(handle.getframerate() or 1)
    except (wave.Error, OSError):
        seconds = request.seconds
    return {'path': backend.root_relative_path(target), 'duration_seconds': round(seconds, 2)}
