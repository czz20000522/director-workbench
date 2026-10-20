"""H3 multi-reference task router.

Thin HTTP surface over :mod:`backend.multiref_tasks`; Lead wires the router in
``app.py``. The task itself is an existing ``kind='workflow'`` task, so
``GET .../multiref-tasks/{task_id}`` returns the normal task shape and the
normal scheduler/runner owns execution and failure/receipt handling.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import multiref_tasks
from ..multiref_recipe import MultiRefError
from ..multiref_tasks import MultiRefTaskRequest


ROUTER = APIRouter()


@ROUTER.post('/api/projects/{project_id}/multiref-tasks', status_code=202)
def start_multiref_task(project_id: str, request: MultiRefTaskRequest) -> dict[str, Any]:
    """Queue one explicit multi-reference H3 generation task.

    Assets are copied into ComfyUI input with controlled names, the graph is
    validated against local ComfyUI, and the normal task runner takes over.
    No candidate is adopted, assembled or downloaded.
    """
    try:
        return multiref_tasks.create_task(project_id, request)
    except multiref_tasks.MultiRefTaskError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except MultiRefError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@ROUTER.get('/api/projects/{project_id}/multiref-tasks/{task_id}')
def get_multiref_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Return an existing task using the normal task result shape."""
    backend.require_project_manifest(project_id)
    try:
        return multiref_tasks.get_task(project_id, task_id)
    except multiref_tasks.MultiRefTaskError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
