"""剧本库的 HTTP 入口：草稿增删改、归档。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import script_library
from ..schemas import ScriptDraftRequest


ROUTER = APIRouter()


@ROUTER.get('/api/script-library')
def list_script_library() -> dict[str, Any]:
    try:
        return script_library.listing()
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@ROUTER.post('/api/script-library/drafts', status_code=201)
def create_script_draft(request: ScriptDraftRequest) -> dict[str, Any]:
    try:
        return {'draft': script_library.create_draft(request.title, request.content)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.put('/api/script-library/drafts/{draft_id}')
def update_script_draft(draft_id: str, request: ScriptDraftRequest) -> dict[str, Any]:
    try:
        return {'draft': script_library.update_draft(draft_id, request.title, request.content)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.delete('/api/script-library/drafts/{draft_id}')
def delete_script_draft(draft_id: str) -> dict[str, Any]:
    try:
        removed = script_library.remove_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not removed:
        raise HTTPException(404, '草稿不存在，可能已被归档或删除')
    return {'removed': True}


@ROUTER.post('/api/script-library/drafts/{draft_id}/archive')
def archive_script_draft(draft_id: str) -> dict[str, Any]:
    try:
        return script_library.archive_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
