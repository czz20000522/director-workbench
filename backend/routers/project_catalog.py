"""作品目录与选择：作品列表、选择、详情、注册退役与任务查询。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from ..schemas import ProjectSelectRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get("/api/projects")
def projects() -> dict[str, Any]:
    catalog = backend.load_project_catalog()
    manifests: list[dict[str, Any]] = []
    principal = backend.current_principal.get()
    selected = backend.PRIVATE_WORKSPACES.selected() if backend.PRIVATE_WORKSPACES is not None and principal else backend.CURRENT_PROJECT_ID
    for entry in backend.project_entries():
        project_id = str(entry.get("id", ""))
        if project_id == getattr(backend, "QUICK_PROJECT_ID", ""):
            # 单步任务的隐藏容器只承载任务与产物，它不是一部作品，不该出现在作品列表里。
            continue
        if backend.PRIVATE_WORKSPACES is not None and principal:
            try:
                backend.PRIVATE_WORKSPACES.require(project_id)
            except HTTPException:
                continue
        try:
            manifest = backend.load_project_manifest(project_id)
        except RuntimeError:
            continue
        summary = backend.project_summary(manifest)
        summary["selected"] = project_id == selected
        manifests.append(summary)
    return {"schema_version": 1, "default_project_id": selected if principal else catalog.get("default_project_id"), "current_project_id": selected, "projects": manifests}


@ROUTER.post("/api/projects/select")
@backend.catalog_transaction
def select_project(request: ProjectSelectRequest) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is not None and backend.current_principal.get() is not None:
        manifest = backend.require_project_manifest(request.project_id)
        backend.PRIVATE_WORKSPACES.select(request.project_id)
        return backend.public_project(manifest)
    backend.ensure_no_release_blocking_tasks("切换项目")
    try:
        manifest = backend.load_project_manifest(request.project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    backend.apply_project_manifest(manifest)
    catalog = backend.load_project_catalog()
    catalog["default_project_id"] = request.project_id
    backend.write_json_atomic(backend.PROJECT_CATALOG_PATH, catalog)
    return backend.public_project()


def require_project_manifest(project_id: str) -> dict[str, Any]:
    try:
        return backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc


@ROUTER.get("/api/projects/{project_id}")
def project_detail(project_id: str) -> dict[str, Any]:
    """Read a work without changing the shared legacy selection."""
    return backend.public_project(backend.require_project_manifest(project_id))


@ROUTER.delete('/api/projects/{project_id}/registration')
def retire_legacy_project(project_id: str) -> dict[str, Any]:
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    principal = backend.current_principal.get()
    if principal is None or not principal.administrator:
        raise HTTPException(404, '作品不存在')
    backend.require_project_manifest(project_id)
    backend.ensure_no_release_blocking_tasks('移除旧作品登记', project_id=project_id)
    backend.PRIVATE_WORKSPACES.retire_legacy_registration(project_id)
    return {'retired_project_id': project_id, 'source_files_preserved': True}


@ROUTER.get("/api/projects/{project_id}/plan")
def project_plan(project_id: str) -> dict[str, Any]:
    return backend.load_project_plan(project_id)[0]


@ROUTER.get("/api/projects/{project_id}/state")
def project_state_detail(project_id: str) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    state = backend.build_project_state(project_id)
    state['readiness'] = backend.project_readiness(project_id, state)
    return state


@ROUTER.get("/api/projects/{project_id}/tasks")
def project_task_list(project_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (project_id, limit + 1, offset)).fetchall()
    more = len(rows) > limit
    values = [backend.task_row(row) for row in rows[:limit]]
    queue_values = backend.TASK_SCHEDULER.queue_info_many([task['id'] for task in values])
    for task in values:
        task['queue'] = queue_values.get(task['id'], {})
    return {"project_id": project_id, "tasks": values, "next_offset": offset + limit if more else None}


@ROUTER.get("/api/projects/{project_id}/tasks/{task_id}")
def project_task_detail(project_id: str, task_id: str) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    task = backend.get_task(task_id)
    if not task or task.get("project_id") != project_id:
        raise HTTPException(404, "该作品中不存在此任务")
    return task
