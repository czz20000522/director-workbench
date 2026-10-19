"""事件流与媒体文件：状态/任务 SSE 事件流、素材与工作流文件下载。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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
    try:
        candidate = backend.resolve_registered_path(path)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    try:
        candidate, identity = checked_file(candidate)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, "素材不存在")
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
