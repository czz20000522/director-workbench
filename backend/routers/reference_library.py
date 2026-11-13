"""本地提示词库与素材参考库的只读索引接口。

检索、拆解和素材查看都不改写原文，也不生成新提示词；这里的每个结果都带
回来源文档与行号。素材只有在用户明确点击后才会复制进当前作品的
``assets/reference-library``，源文件始终不动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
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


class SaveFirstFrameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    title: str = Field(default='', max_length=48)


class HideRequest(BaseModel):
    kind: str = Field(max_length=20)
    id: str = Field(min_length=1, max_length=120)


class RestoreRequest(BaseModel):
    kind: str = Field(max_length=20)
    id: str | None = Field(default=None, max_length=120)


@ROUTER.get('/api/reference-library')
def reference_library_overview() -> dict[str, Any]:
    return reference_library.overview(backend.ROOT)


@ROUTER.get('/api/reference-library/libraries')
def reference_library_libraries() -> dict[str, Any]:
    """子库清单：给独立页面左侧的库导航用。"""
    return reference_library.libraries(backend.ROOT)


@ROUTER.get('/api/reference-library/library')
def reference_library_browse(category: str = '', q: str = '', offset: int = 0, limit: int = 60) -> dict[str, Any]:
    """单个子库的分页素材，供独立页面滚动加载。"""
    return reference_library.browse(backend.ROOT, category.strip(), q, offset, limit)


@ROUTER.post('/api/reference-library/hide')
def hide_reference_item(request: "HideRequest") -> dict[str, Any]:
    """从库里移除一个条目；源文件和原文段落都不动，只记一份排除名单。"""
    try:
        return reference_library.hide(backend.ROOT, request.kind, request.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.post('/api/reference-library/restore')
def restore_reference_items(request: "RestoreRequest") -> dict[str, Any]:
    """恢复一条（带 id）或整类（不带 id）被移除的条目。"""
    try:
        return reference_library.restore(backend.ROOT, request.kind, request.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


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
def reference_library_anatomy(request: "AnatomyRequest") -> dict[str, Any]:
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
def reference_library_stage_material(material_id: str, request: "StageMaterialRequest") -> dict[str, Any]:
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


@ROUTER.post('/api/reference-library/materials/{material_id}/variants')
async def reference_library_material_variant(material_id: str, file: UploadFile = File(...),
                                            title: str = Form('')) -> dict[str, Any]:
    """把液化/精修后的 PNG 存回这张素材所在的子库，作为一张新素材。

    原图不动：库里那张通常是挑过的，覆盖掉就再也找不回来。新文件名带时间戳，
    子库分类沿用原素材，所以保存后在同一个小库里就能看到。
    """
    payload = await file.read(reference_library.VARIANT_MAX_BYTES + 1)
    try:
        material = reference_library.save_material_variant(backend.ROOT, material_id, payload, title)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f'素材保存失败：{exc}') from exc
    return {'material': material, 'path': material['path']}


@ROUTER.post('/api/reference-library/first-frames')
def reference_library_save_first_frame(request: "SaveFirstFrameRequest") -> dict[str, Any]:
    """把生成出来的首帧存进素材库的「首帧图参考」，下次可直接选作开始画面。"""
    try:
        source = backend.resolve_registered_path(request.path)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f'这张图不在允许读取的目录里：{exc}') from exc
    try:
        material = reference_library.save_first_frame(backend.ROOT, backend.ASSETS_ROOT, source, request.title)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f'首帧图保存失败：{exc}') from exc
    return {'material': material, 'path': material['path'],
            'library': reference_library.FIRST_FRAME_LIBRARY}
