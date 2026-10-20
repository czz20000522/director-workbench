"""Series-level reusable resource packs.

A pack lives inside one series directory::

    <account>/series/<series>/_资源包/<pack_id>/pack.json
    <account>/series/<series>/_资源包/<pack_id>/assets/<file>

Packs are explicit registrations owned by the signed-in account.  This module
never creates resources, binds them to a project or downloads models; it only
records media that already exists inside a project workspace and copies that
file into the pack so a published pack is self-contained.

Ownership and series checks are strict:

* the series directory must be an existing direct child of the caller's
  ``series`` directory;
* a pack id is an ASCII identifier and every path is resolved through
  ``UserContext.path`` so symlinks and ``..`` traversal are rejected;
* a project reference is only accepted when the caller owns the project and the
  project's series is the series that owns the pack;
* a referenced media file must resolve inside that project's workspace.

The pack is mutable while ``status`` is ``draft`` or ``reviewed``.  Publishing
sets ``status`` to ``published`` and freezes the pack for every later write.
``version`` is 0 while the pack is a draft and becomes 1 on the first publish;
because a published pack is frozen there is no second version yet.

Recorded fields are the canonical contract for this module:

* pack: ``id``, ``kind`` (``character``/``scene``/``prop``/``sound``),
  ``title``, ``version``, ``status`` (``draft``/``reviewed``/``published``),
  ``assets``, ``created_at`` (epoch seconds), plus ``updated_at`` and
  ``published_at`` bookkeeping;
* asset: ``asset_id``, ``role``, ``view``, ``media_ref`` (POSIX path relative
  to the pack directory), ``filename`` (the original media name), ``sha256``
  and ``size`` of the copied bytes.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

import hashlib
import json
import os
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from fastapi import HTTPException

from .private_auth import current_principal
from .user_context import AccessDenied, UserContext, directory_label, identifier
from .workbench_config import MATERIAL_EXTENSIONS

PACK_DIRECTORY = '_资源包'
PACK_FILE = 'pack.json'
ASSETS_DIRECTORY = 'assets'
PACK_KINDS = ('character', 'scene', 'prop', 'sound')
PACK_STATUSES = ('draft', 'reviewed', 'published')
MUTABLE_STATUSES = frozenset({'draft', 'reviewed'})
MEDIA_KINDS = ('image', 'audio', 'video')

MAX_PACK_ID = 128
MAX_PROJECT_ID = 128
MAX_TITLE = 200
MAX_LABEL = 80
MAX_MEDIA_REF = 1024
COPY_CHUNK_BYTES = 1024 * 1024

_LOCK = threading.RLock()


class ResourcePackCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = Field(default=None, max_length=MAX_PACK_ID)
    kind: Literal['character', 'scene', 'prop', 'sound']
    title: str = Field(min_length=1, max_length=MAX_TITLE)


class ResourcePackAssetCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str = Field(min_length=1, max_length=MAX_PROJECT_ID)
    role: str = Field(default='', max_length=MAX_LABEL)
    view: str = Field(default='', max_length=MAX_LABEL)
    media_ref: str | None = Field(default=None, max_length=MAX_MEDIA_REF)
    asset_id: str | None = Field(default=None, max_length=128)



def _backend():
    """Read the workbench object at call time so app-level monkeypatches keep working."""
    from . import app as backend
    return backend


def _require_context() -> tuple[Any, Any, UserContext]:
    backend = _backend()
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    principal = current_principal.get()
    if principal is None:
        raise HTTPException(401, '请登录工作台')
    return backend, backend.PRIVATE_WORKSPACES, UserContext(principal.identity.username, backend.PRIVATE_WORKSPACES.layout)


def _clean_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise HTTPException(422, f'{label}必须是文本')
    text = value.strip()
    if required and not text:
        raise HTTPException(422, f'{label}不能为空')
    if len(text) > maximum:
        raise HTTPException(422, f'{label}不能超过 {maximum} 个字符')
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise HTTPException(422, f'{label}不能包含控制字符')
    return text


def _safe_filename(name: str) -> str:
    cleaned = ''.join(
        char for char in str(name)
        if unicodedata.category(char)[0] != 'C' and char not in '<>:"/\\|?*'
    ).strip(' .')
    if not cleaned:
        cleaned = 'asset'
    stem, suffix = os.path.splitext(cleaned)
    stem = stem[:80].strip(' .') or 'asset'
    if stem.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        stem = '_' + stem
    return stem + suffix[:16]


def _pack_identifier(pack_id: Any) -> str:
    if not isinstance(pack_id, str):
        raise HTTPException(422, '资源包标识无效')
    try:
        return identifier(pack_id)
    except AccessDenied as exc:
        raise HTTPException(422, '资源包标识无效：只能使用字母、数字、下划线和连字符') from exc


def _project_identifier(project_id: Any) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise HTTPException(422, 'project_id 无效')
    try:
        return identifier(project_id.strip())
    except AccessDenied as exc:
        raise HTTPException(422, 'project_id 无效') from exc


def _series_directory(context: UserContext, series: str) -> Path:
    if not isinstance(series, str) or not series or len(series) > 160:
        raise HTTPException(422, '系列名称无效')
    if series in {'.', '..'} or series.startswith('.') or any(char in series for char in '/\\') or any(ord(char) < 32 for char in series):
        raise HTTPException(422, '系列名称无效')
    try:
        root = context.path('series')
        candidate = context.path(root / series)
    except (AccessDenied, ValueError, OSError) as exc:
        raise HTTPException(422, '系列路径非法') from exc
    if candidate.parent != root:
        raise HTTPException(422, '系列路径越界')
    if not candidate.is_dir():
        raise HTTPException(404, '系列不存在')
    return candidate


def _pack_directory(context: UserContext, series: str, pack_id: str) -> Path:
    series_dir = _series_directory(context, series)
    safe_id = _pack_identifier(pack_id)
    try:
        base = context.path(series_dir / PACK_DIRECTORY)
        pack_dir = context.path(base / safe_id)
    except (AccessDenied, ValueError, OSError) as exc:
        raise HTTPException(422, '资源包路径非法') from exc
    if pack_dir.parent != base:
        raise HTTPException(422, '资源包路径越界')
    return pack_dir


def _validate_pack(document: Any, pack_id: str) -> dict[str, Any]:
    invalid = HTTPException(409, f'资源包 {pack_id} 数据损坏，请人工核对')
    if not isinstance(document, dict) or document.get('id') != pack_id:
        raise invalid
    if document.get('kind') not in PACK_KINDS:
        raise invalid
    title = document.get('title')
    if not isinstance(title, str) or not title.strip():
        raise invalid
    version = document.get('version')
    if type(version) is not int or version < 0:
        raise invalid
    if document.get('status') not in PACK_STATUSES:
        raise invalid
    created_at = document.get('created_at')
    if type(created_at) not in (int, float) or not (created_at > 0):
        raise invalid
    assets = document.get('assets')
    if not isinstance(assets, list):
        raise invalid
    seen: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise invalid
        asset_id = asset.get('asset_id')
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen:
            raise invalid
        seen.add(asset_id)
        if not isinstance(asset.get('role'), str) or not isinstance(asset.get('view'), str):
            raise invalid
        media_ref = asset.get('media_ref')
        filename = asset.get('filename')
        if not isinstance(media_ref, str) or not media_ref or not isinstance(filename, str) or not filename:
            raise invalid
        digest = asset.get('sha256')
        if not isinstance(digest, str) or len(digest) != 64:
            raise invalid
        size = asset.get('size')
        if type(size) is not int or size < 0:
            raise invalid
    return document


def _read_pack(context: UserContext, series: str, pack_id: str) -> tuple[Path, dict[str, Any]]:
    pack_dir = _pack_directory(context, series, pack_id)
    if not pack_dir.is_dir():
        raise HTTPException(404, '资源包不存在')
    document_path = pack_dir / PACK_FILE
    if not document_path.is_file():
        raise HTTPException(409, '资源包数据不完整，请人工核对')
    try:
        document = json.loads(document_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise HTTPException(409, '资源包数据损坏，请人工核对') from exc
    return pack_dir, _validate_pack(document, pack_id)


def _write_pack(pack_dir: Path, document: dict[str, Any]) -> None:
    document['updated_at'] = time.time()
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    target = pack_dir / PACK_FILE
    temporary = pack_dir / f'.{uuid.uuid4().hex}.pack.tmp'
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_project(backend: Any, project_id: str) -> dict[str, Any]:
    try:
        return backend.load_project_manifest(project_id)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(404, '作品不存在') from exc


def _project_workspace(backend: Any, manifest: dict[str, Any]) -> Path:
    value = manifest.get('workspace_root')
    if not value:
        raise HTTPException(409, '作品缺少工作目录登记')
    try:
        workspace = backend.resolve_registered_path(str(value))
    except HTTPException:
        raise
    except (ValueError, OSError) as exc:
        raise HTTPException(409, '作品目录不可用') from exc
    if not workspace.is_dir():
        raise HTTPException(409, '作品目录不存在')
    return workspace.resolve()


def _media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    for kind in MEDIA_KINDS:
        if suffix in MATERIAL_EXTENSIONS[kind]:
            return kind
    return 'other'


def _resolve_workspace_media(context: UserContext, workspace: Path, value: str) -> Path:
    raw = value.strip()
    if not raw or len(raw) > MAX_MEDIA_REF:
        raise HTTPException(422, '媒体引用无效')
    if '://' in raw or raw.startswith(('\\\\', '//')):
        raise HTTPException(422, '媒体引用必须是作品内的本地文件')
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw.replace('\\', '/'))
    if windows_path.drive and not windows_path.is_absolute():
        raise HTTPException(422, '媒体引用不能使用盘符相对路径')
    if windows_path.is_absolute() or posix_path.is_absolute():
        candidate = Path(raw)
    else:
        if '..' in posix_path.parts or '..' in windows_path.parts:
            raise HTTPException(422, '媒体引用不能包含上级目录')
        candidate = workspace / Path(*posix_path.parts)
    try:
        resolved = context.path(candidate)
    except AccessDenied as exc:
        raise HTTPException(422, '媒体文件不属于当前作品') from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, '媒体引用非法') from exc
    if not resolved.is_relative_to(workspace):
        raise HTTPException(422, '媒体文件不属于当前作品')
    if not resolved.is_file():
        raise HTTPException(404, '媒体文件不存在')
    if _media_kind(resolved) not in MEDIA_KINDS:
        raise HTTPException(422, '只支持登记图片、音频或视频媒体')
    return resolved


def _resolve_media(
    backend: Any,
    context: UserContext,
    manifest: dict[str, Any],
    workspace: Path,
    *,
    media_ref: str | None,
    asset_id: str | None,
) -> Path:
    if asset_id is not None:
        target_id = str(asset_id).strip()
        if not target_id or len(target_id) > 128:
            raise HTTPException(422, 'asset_id 无效')
        asset = next(
            (item for item in manifest.get('assets') or [] if isinstance(item, dict) and str(item.get('id')) == target_id),
            None,
        )
        if asset is None:
            raise HTTPException(404, '作品中不存在这个素材')
        references: list[str] = []
        image = asset.get('image_path')
        if isinstance(image, str) and image:
            references.append(image)
        sources = asset.get('sources')
        if isinstance(sources, dict):
            references.extend(str(item) for item in sources.values() if isinstance(item, str) and item)
        references = list(dict.fromkeys(references))
        if not references:
            raise HTTPException(422, '该素材没有登记可用的媒体文件')
        if len(references) > 1:
            raise HTTPException(422, '该素材包含多个媒体文件，请用 media_ref 明确指定')
        return _resolve_workspace_media(context, workspace, references[0])
    if not isinstance(media_ref, str):
        raise HTTPException(422, 'media_ref 必须是文本')
    return _resolve_workspace_media(context, workspace, media_ref)


def _copy_into_pack(context: UserContext, pack_dir: Path, source: Path) -> tuple[Path, str, int]:
    assets_dir = context.path(pack_dir / ASSETS_DIRECTORY)
    if assets_dir.exists() and not assets_dir.is_dir():
        raise HTTPException(409, '资源包 assets 目录不可用')
    assets_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(source.name)
    destination = assets_dir / filename
    counter = 2
    while destination.exists():
        destination = assets_dir / f'{Path(filename).stem}-{counter}{Path(filename).suffix}'
        counter += 1
    temporary = context.path(assets_dir / f'.{uuid.uuid4().hex}.asset.tmp')
    digest = hashlib.sha256()
    size = 0
    try:
        before = source.stat()
        with source.open('rb') as incoming, temporary.open('xb') as outgoing:
            while chunk := incoming.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
                outgoing.write(chunk)
                size += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
            raise HTTPException(409, '媒体文件在登记期间发生变化，请重试')
        if size == 0:
            raise HTTPException(422, '媒体文件为空')
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination, digest.hexdigest(), size


def _list_valid_packs(context: UserContext, series: str) -> tuple[list[dict[str, Any]], list[str]]:
    series_dir = _series_directory(context, series)
    base = context.path(series_dir / PACK_DIRECTORY)
    if not base.is_dir():
        return [], []
    packs: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for entry in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
        if not entry.is_dir():
            unreadable.append(entry.name)
            continue
        try:
            _, document = _read_pack(context, series, entry.name)
        except HTTPException:
            unreadable.append(entry.name)
            continue
        packs.append(document)
    packs.sort(key=lambda item: (float(item.get('created_at') or 0), str(item.get('id'))), reverse=True)
    return packs, unreadable


def list_packs(series: str) -> dict[str, Any]:
    _, _, context = _require_context()
    packs, unreadable = _list_valid_packs(context, series)
    return {'series': series, 'packs': packs, 'unreadable': unreadable}


def get_pack(series: str, pack_id: str) -> dict[str, Any]:
    _, _, context = _require_context()
    _, document = _read_pack(context, series, pack_id)
    return {'series': series, 'pack': document}


def asset_file(series: str, pack_id: str, asset_id: str) -> Path:
    """Resolve one asset file inside a pack without accepting arbitrary paths."""
    backend, _, context = _require_context()
    _series_directory(context, series)
    pack_dir, document = _read_pack(context, series, _pack_identifier(pack_id))
    asset = next((item for item in document.get('assets', []) if item.get('asset_id') == asset_id), None)
    if asset is None:
        raise HTTPException(404, '资源包中不存在此资产')
    relative = str(asset.get('media_ref') or '')
    if not relative or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts:
        raise HTTPException(409, '资源包资产路径无效')
    path = context.path(pack_dir / relative)
    assets_root = context.path(pack_dir / ASSETS_DIRECTORY)
    if not path.is_file() or not path.is_relative_to(assets_root):
        raise HTTPException(404, '资源包资产不可读')
    return path


def create_pack(series: str, *, pack_id: str | None, kind: str, title: str) -> dict[str, Any]:
    _, _, context = _require_context()
    if kind not in PACK_KINDS:
        raise HTTPException(422, f"资源包类型必须是 {'/'.join(PACK_KINDS)} 之一")
    clean_title = _clean_text(title, label='资源包标题', maximum=MAX_TITLE, required=True)
    safe_id = _pack_identifier(pack_id) if pack_id is not None else f'pack-{uuid.uuid4().hex[:12]}'
    with _LOCK:
        series_dir = _series_directory(context, series)
        try:
            base = context.path(series_dir / PACK_DIRECTORY)
            if base.exists() and not base.is_dir():
                raise HTTPException(409, '资源包目录不可用')
            if not base.exists():
                base.mkdir(parents=True, exist_ok=True)
            pack_dir = context.path(base / safe_id)
            if pack_dir.exists():
                raise HTTPException(409, '这个资源包已经存在')
            now = time.time()
            document = {
                'id': safe_id,
                'kind': kind,
                'title': clean_title,
                'version': 0,
                'status': 'draft',
                'assets': [],
                'created_at': now,
                'updated_at': now,
            }
            pack_dir.mkdir(exist_ok=False)
            (pack_dir / ASSETS_DIRECTORY).mkdir(exist_ok=False)
            _write_pack(pack_dir, document)
        except FileExistsError as exc:
            raise HTTPException(409, '这个资源包已经存在') from exc
        except HTTPException:
            raise
        except (OSError, ValueError) as exc:
            if 'pack_dir' in locals():
                shutil.rmtree(pack_dir, ignore_errors=True)
            raise HTTPException(500, '资源包创建失败，请检查磁盘后重试') from exc
        return {'series': series, 'pack': document}


def add_asset(
    series: str,
    pack_id: str,
    *,
    project_id: str,
    role: str = '',
    view: str = '',
    media_ref: str | None = None,
    asset_id: str | None = None,
) -> dict[str, Any]:
    backend, _, context = _require_context()
    project_key = _project_identifier(project_id)
    clean_role = _clean_text(role, label='资源角色', maximum=MAX_LABEL)
    clean_view = _clean_text(view, label='资源视角', maximum=MAX_LABEL)
    if (media_ref is None) == (asset_id is None):
        raise HTTPException(422, '必须且只能提供 media_ref 或 asset_id 之一')
    with _LOCK:
        pack_dir, document = _read_pack(context, series, pack_id)
        if document['status'] not in MUTABLE_STATUSES:
            raise HTTPException(409, '资源包已发布并冻结，不能继续添加资源')
        manifest = _load_project(backend, project_key)
        project_series = directory_label(str(manifest.get('series') or ''), '未命名系列')
        if project_series != series:
            raise HTTPException(409, '作品不属于这个系列，不能登记到该资源包')
        workspace = _project_workspace(backend, manifest)
        source = _resolve_media(backend, context, manifest, workspace, media_ref=media_ref, asset_id=asset_id)
        copied, digest, size = _copy_into_pack(context, pack_dir, source)
        existing = next(
            (item for item in document['assets']
             if item.get('sha256') == digest and item.get('role') == clean_role and item.get('view') == clean_view),
            None,
        )
        if existing is not None:
            copied.unlink(missing_ok=True)
            if not (pack_dir / str(existing.get('media_ref', ''))).is_file():
                raise HTTPException(409, '资源包中的媒体文件缺失，请人工核对')
            return {'series': series, 'pack': document, 'asset': existing, 'created': False}
        asset = {
            'asset_id': f'asset-{uuid.uuid4().hex[:16]}',
            'role': clean_role,
            'view': clean_view,
            'media_ref': f'{ASSETS_DIRECTORY}/{copied.name}',
            'filename': _safe_filename(source.name),
            'sha256': digest,
            'size': size,
        }
        document['assets'].append(asset)
        try:
            _write_pack(pack_dir, document)
        except BaseException:
            copied.unlink(missing_ok=True)
            raise
        return {'series': series, 'pack': document, 'asset': asset, 'created': True}


def publish_pack(series: str, pack_id: str) -> dict[str, Any]:
    _, _, context = _require_context()
    with _LOCK:
        pack_dir, document = _read_pack(context, series, pack_id)
        if document['status'] == 'published':
            return {'series': series, 'pack': document, 'published': False}
        if document['status'] not in MUTABLE_STATUSES:
            raise HTTPException(409, '资源包状态不允许发布')
        document['status'] = 'published'
        document['version'] = int(document.get('version') or 0) + 1
        document['published_at'] = time.time()
        _write_pack(pack_dir, document)
        return {'series': series, 'pack': document, 'published': True}



def delete_pack(series: str, pack_id: str, *, confirm: bool = False) -> dict[str, Any]:
    """Delete one whole resource pack after an explicit confirmation.

    Published packs are user-owned data, so this is deliberately not a silent
    cleanup primitive: the caller must pass ``confirm=true`` and the pack is
    removed directory-first, not merged into another pack.
    """
    _, _, context = _require_context()
    pack_dir, document = _read_pack(context, series, _pack_identifier(pack_id))
    if not confirm:
        raise HTTPException(409, {
            'code': 'confirm_pack_delete',
            'message': '删除资源包需要显式确认',
            'pack_id': document.get('id'),
            'title': document.get('title'),
            'assets': len(document.get('assets') or []),
        })
    with _LOCK:
        pack_dir, document = _read_pack(context, series, _pack_identifier(pack_id))
        shutil.rmtree(pack_dir)
    return {
        'deleted': True,
        'series': series,
        'pack_id': document.get('id'),
        'title': document.get('title'),
        'asset_count': len(document.get('assets') or []),
    }

def available_for_project(project_id: str) -> dict[str, Any]:
    backend, _, context = _require_context()
    project_key = _project_identifier(project_id)
    manifest = _load_project(backend, project_key)
    series = str(manifest.get('series') or '').strip()
    if not series:
        raise HTTPException(409, '作品没有系列登记，无法列出资源包')
    series_directory = directory_label(series, '未命名系列')
    series_path = context.path(context.path('series') / series_directory)
    if not series_path.is_dir():
        return {'project_id': project_key, 'series': series, 'packs': [], 'unreadable': []}
    packs, unreadable = _list_valid_packs(context, series_directory)
    available = [item for item in packs if item['status'] == 'published']
    return {'project_id': project_key, 'series': series, 'packs': available, 'unreadable': unreadable}
