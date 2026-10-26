"""本地提示词库与素材参考库的只读索引接口。

检索、拆解和素材查看都不改写原文，也不生成新提示词；这里的每个结果都带
回来源文档与行号。素材只有在用户明确点击后才会复制进当前作品的
``assets/reference-library``，源文件始终不动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import app as backend
from .. import reference_library
from ..secure_files import VerifiedFileResponse, checked_file


ROUTER = APIRouter()


class AnatomyRequest(BaseModel):
    text: str = Field(max_length=40000)


class StageMaterialRequest(BaseModel):
    project_id: str


@ROUTER.get('/api/reference-library')
def reference_library_overview() -> dict[str, Any]:
    return reference_library.overview(backend.ROOT)


@ROUTER.post('/api/reference-library/refresh')
def reference_library_refresh() -> dict[str, Any]:
    reference_library.invalidate(backend.ROOT)
    return reference_library.overview(backend.ROOT)


@ROUTER.get('/api/reference-library/search')
def reference_library_search(q: str = '', limit: int = 12, kind: str = '', work: str = '', document: str = '') -> dict[str, Any]:
    return reference_library.search(
        backend.ROOT, q, max(1, min(limit, 50)),
        kind.strip() or None, work.strip() or None, document.strip() or None,
    )


@ROUTER.get('/api/reference-library/entries/{entry_id}')
def reference_library_entry(entry_id: str) -> dict[str, Any]:
    entry = reference_library.find_entry(backend.ROOT, entry_id)
    if entry is None:
        raise HTTPException(404, '提示词条目不存在')
    return {
        'id': entry['id'],
        'title': entry['title'],
        'kind': entry['kind'],
        'work': entry.get('work', '未归类'),
        'tags': entry['tags'],
        'document_id': entry['document_id'],
        'document_title': entry['document_title'],
        'document_path': entry['document_path'],
        'line': entry['line'],
        'body': entry['body'],
        'plain': entry['plain'],
    }


@ROUTER.post('/api/reference-library/anatomy')
def reference_library_anatomy(request: AnatomyRequest) -> dict[str, Any]:
    return reference_library.anatomy(backend.ROOT, request.text)


@ROUTER.get('/api/reference-library/materials/{material_id}/thumb')
def reference_library_material_thumb(material_id: str) -> FileResponse:
    try:
        candidate, _ = reference_library.thumbnail_file(backend.ROOT, material_id)
        candidate, identity = checked_file(candidate)
    except FileNotFoundError as exc:
        raise HTTPException(404, '素材不存在') from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(404, '素材无法读取') from exc
    return VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat(), headers={'Cache-Control': 'private, max-age=300'})


@ROUTER.get('/api/reference-library/materials/{material_id}/file')
def reference_library_material_file(material_id: str) -> FileResponse:
    try:
        candidate, _ = reference_library.resolve_material_file(backend.ROOT, material_id)
        candidate, identity = checked_file(candidate)
    except FileNotFoundError as exc:
        raise HTTPException(404, '素材不存在') from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(404, '素材无法读取') from exc
    return VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat(), headers={'Cache-Control': 'private, max-age=60'})


@ROUTER.post('/api/reference-library/materials/{material_id}/stage')
def reference_library_stage_material(material_id: str, request: StageMaterialRequest) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(request.project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get('workspace_root')
    workspace = (
        backend.resolve_registered_path(str(workspace_value))
        if workspace_value
        else (backend.DIRECTOR_WORKSPACES_ROOT / request.project_id).resolve()
    )
    try:
        destination, material = reference_library.stage_material(backend.ROOT, material_id, workspace)
        registered = backend.root_relative_path(destination)
    except FileNotFoundError as exc:
        raise HTTPException(404, '素材不存在') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f'素材复制失败：{exc}') from exc
    return {'path': registered, 'material': reference_library.public_material(material)}
