"""HTTP surface for one-stage speech + H3 dialogue shots.

The speech model is an internal stage.  This router exposes one task per shot,
not a second public speech task.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import dialogue_shot_tasks
from ..dialogue_shot_tasks import DialogueShotRequest


ROUTER = APIRouter()


@ROUTER.post('/api/projects/{project_id}/dialogue-shots', status_code=202)
def start_dialogue_shot(project_id: str, request: DialogueShotRequest) -> dict[str, Any]:
    try:
        return dialogue_shot_tasks.create_task(project_id, request)
    except dialogue_shot_tasks.DialogueShotError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@ROUTER.get('/api/projects/{project_id}/dialogue-shots/{task_id}')
def get_dialogue_shot(project_id: str, task_id: str) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    try:
        return dialogue_shot_tasks.get_task(project_id, task_id)
    except dialogue_shot_tasks.DialogueShotError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
