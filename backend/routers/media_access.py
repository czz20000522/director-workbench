"""受控媒体引用：稳定引用、短期签名 URL 与按作品读取媒体文件。

远程 Agent 只能拿到不含服务器路径的 ``media_ref`` 和有时效的下载 URL；
URL 仍要求本人 Bearer 会话，并由私人业务层先校验作品归属。这里不提供
任意路径下载：每个目标都从本作品 manifest、计划或参考拆解中解析。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from .. import media_refs
from .. import app as backend
from ..schemas import MediaRefsRequest
from ..secure_files import VerifiedFileResponse, checked_file


ROUTER = APIRouter()


@ROUTER.post('/api/projects/{project_id}/media-refs')
def issue_media_refs(project_id: str, request: MediaRefsRequest) -> dict[str, Any]:
    """为一组稳定媒体引用签发短期受控 URL；批量中单项失败不影响其他项。"""
    backend.require_project_manifest(project_id)
    try:
        refs = media_refs.issue_many(project_id, request.media_refs, ttl_seconds=request.ttl_seconds)
    except media_refs.MediaRefError as exc:
        raise HTTPException(422, {'code': 'invalid_media_refs', 'message': str(exc), 'retryable': False}) from exc
    return {'refs': refs}


@ROUTER.get('/api/projects/{project_id}/media')
def list_media(
    project_id: str,
    category: Literal['all', 'assets', 'segments', 'assembly', 'references', 'tasks'] = 'all',
    kind: Literal['all', 'image', 'audio', 'video', 'file'] = 'all',
    limit: int = 50,
    offset: int = 0,
    ttl_seconds: int = media_refs.TOKEN_TTL_SECONDS,
) -> dict[str, Any]:
    """列出本作品可读取的媒体事实（稳定引用 + 短期 URL），不返回服务器路径。"""
    backend.require_project_manifest(project_id)
    try:
        return media_refs.list_project_media(project_id, category=category, kind=kind,
                                             limit=limit, offset=offset, ttl_seconds=ttl_seconds)
    except media_refs.MediaRefError as exc:
        raise HTTPException(422, {'code': 'invalid_media_query', 'message': str(exc), 'retryable': False}) from exc


@ROUTER.get('/api/projects/{project_id}/media/{token}')
def read_media(project_id: str, token: str) -> VerifiedFileResponse:
    """按签名 URL 返回本作品媒体文件；失效/伪造 token 不解析任何路径。"""
    backend.require_project_manifest(project_id)
    try:
        target = media_refs.resolve_token(project_id, token)
    except media_refs.MediaRefExpired as exc:
        raise HTTPException(410, {'code': 'media_ref_expired', 'message': str(exc), 'retryable': False}) from exc
    except media_refs.MediaRefError as exc:
        raise HTTPException(404, {'code': 'media_ref_invalid', 'message': str(exc), 'retryable': False}) from exc
    try:
        resolved = media_refs.resolve_target(project_id, target)
    except media_refs.MediaRefError as exc:
        raise HTTPException(404, {'code': 'media_unavailable', 'message': str(exc), 'retryable': False}) from exc
    try:
        candidate, identity = checked_file(resolved['path'])
        stat = candidate.stat()
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, {'code': 'media_unavailable', 'message': '媒体文件不存在或不可读取', 'retryable': False}) from exc
    return VerifiedFileResponse(candidate, identity=identity, filename=resolved['filename'],
                                media_type=resolved['content_type'], stat_result=stat)
