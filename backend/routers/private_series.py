"""私有系列与作品目录：私人工作区的系列树、归档媒体与删除计划。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException
from .. import private_deletion
from ..schemas import DirectoryDeleteRequest, SeriesCreateRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/private/series')
def list_private_series() -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    return {'series': backend.PRIVATE_WORKSPACES.series_tree()}


@ROUTER.post('/api/private/series', status_code=201)
def create_private_series(request: SeriesCreateRequest) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    return backend.PRIVATE_WORKSPACES.create_series(request.name)


@ROUTER.get('/api/private/series/{series_name}/works/{work_name}/media')
def list_private_archive_media(series_name: str, work_name: str) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    try:
        return backend.PRIVATE_WORKSPACES.archive_media(series_name, work_name)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(404, '目录不存在') from exc


def private_delete_target(series_name: str, work_name: str | None = None) -> tuple[Path, Path]:
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    try:
        return backend.PRIVATE_WORKSPACES.deletion_target(series_name, work_name)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(404, '目录不存在') from exc


@ROUTER.post('/api/private/series/{series_name}/deletion-plan')
def plan_series_deletion(series_name: str) -> dict[str, Any]:
    root, target = backend.private_delete_target(series_name)
    return private_deletion.plan(root, target, backend.RUNTIME / 'deletion-plans')


@ROUTER.delete('/api/private/series/{series_name}')
def delete_series(series_name: str, request: DirectoryDeleteRequest) -> dict[str, Any]:
    root, target = backend.private_delete_target(series_name)
    if request.confirm != series_name:
        raise HTTPException(422, '请填写完整系列名称以确认删除')
    backend.ensure_no_release_blocking_tasks('删除系列')
    result = private_deletion.apply(root, target, backend.RUNTIME / 'deletion-plans', request.plan_id)
    backend.PRIVATE_WORKSPACES.retire_deleted(series_name)
    return result


@ROUTER.post('/api/private/series/{series_name}/works/{work_name}/deletion-plan')
def plan_work_deletion(series_name: str, work_name: str) -> dict[str, Any]:
    root, target = backend.private_delete_target(series_name, work_name)
    return private_deletion.plan(root, target, backend.RUNTIME / 'deletion-plans')


@ROUTER.delete('/api/private/series/{series_name}/works/{work_name}')
def delete_work(series_name: str, work_name: str, request: DirectoryDeleteRequest) -> dict[str, Any]:
    root, target = backend.private_delete_target(series_name, work_name)
    if request.confirm != work_name:
        raise HTTPException(422, '请填写完整作品目录名称以确认删除')
    backend.ensure_no_release_blocking_tasks('删除作品')
    result = private_deletion.apply(root, target, backend.RUNTIME / 'deletion-plans', request.plan_id)
    backend.PRIVATE_WORKSPACES.retire_deleted(series_name, work_name)
    return result
