"""剧本库的 HTTP 入口：草稿增删改、归档。"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import script_library
from .. import prompt_writer
from ..schemas import (ScriptDraftRequest, ScriptArchiveDeleteRequest,
                       ScriptBreakdownExtractRequest, ScriptBreakdownSaveRequest)


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


@ROUTER.get('/api/script-library/drafts/{draft_id}')
def get_script_draft(draft_id: str) -> dict[str, Any]:
    """草稿详情：正文 + 素材清单（列表接口不带清单，避免每次刷新都拖着几十条）。"""
    try:
        return {'draft': script_library.public_draft(script_library.get_draft(draft_id))}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@ROUTER.post('/api/script-library/drafts/{draft_id}/breakdown')
def extract_script_breakdown(draft_id: str, request: ScriptBreakdownExtractRequest) -> dict[str, Any]:
    """读一遍正文，拆成角色/场景/道具/音色/配乐清单。

    只出建议，不生成任何素材；返回的清单还要用户在界面上改、勾、确认。
    """
    try:
        entry = script_library.get_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    content = str(entry.get('content') or '').strip()
    if len(content) < 20:
        raise HTTPException(422, '正文太短（不足 20 字），先写够一场戏再拆')
    try:
        result = prompt_writer.breakdown(content, provider=request.provider)
    except prompt_writer.WriterUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (prompt_writer.WriterFailed, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    try:
        parsed = script_library.parse_breakdown_json(str(result.get('text') or ''))
        items = script_library.items_from_extraction(parsed)
    except ValueError as exc:
        raise HTTPException(502, f'{exc}；可以让模型再试一次，或改用另一条通道') from exc
    if not items:
        raise HTTPException(422, '这段剧本里没拆出可用的素材条目')
    saved = script_library.save_breakdown(
        draft_id, items, confirmed=False,
        meta={'source_hash': hashlib.sha1(content.encode('utf-8')).hexdigest(),
              'source_chars': int(result.get('source_chars') or len(content)),
              'truncated': bool(result.get('truncated')),
              'provider': result.get('provider'), 'model': result.get('model'),
              'elapsed_ms': result.get('elapsed_ms'), 'extracted_at': time.time()})
    return {'draft': saved, 'count': len(saved['breakdown']['items'])}


@ROUTER.put('/api/script-library/drafts/{draft_id}/breakdown')
def save_script_breakdown(draft_id: str, request: ScriptBreakdownSaveRequest) -> dict[str, Any]:
    try:
        saved = script_library.save_breakdown(
            draft_id, [item.model_dump() for item in request.items], confirmed=request.confirmed)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'draft': saved}


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


@ROUTER.post('/api/script-library/archived/restore')
def restore_archived_script(request: ScriptArchiveDeleteRequest) -> dict[str, Any]:
    """把一份已归档剧本收回草稿：正文进草稿区，归档文件移入回收站（可还原）。"""
    try:
        return script_library.restore_archived(request.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@ROUTER.post('/api/script-library/archived/delete')
def delete_archived_script(request: ScriptArchiveDeleteRequest) -> dict[str, Any]:
    """删掉一份已归档剧本；归档区是唯一副本，所以删前由界面确认。"""
    try:
        return script_library.remove_archived(request.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
