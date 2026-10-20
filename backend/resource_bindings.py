"""显式分镜资源包绑定与资源包首帧使用。

合同（第一版）：

* ``GET /api/projects/{project_id}/segments/{segment_id}/resources``
  回显该分镜已显式绑定的资源包事实（同系列、已发布、当前用户可见），并列出
  本系列已发布资源包摘要。只读，不生成、不改计划。
* ``PUT .../resources``
  调用方显式给出 ``character_pack_ids``、``scene_pack_id``、``sound_pack_id``
  和可选的 ``asset_ids``；校验通过后只写入 ``plan.segment.resources`` 的
  ``pack_id / version / asset_ids``，不提交生成、不自动设置首帧或使用资源。
* ``POST .../use-resource-frame``
  从已发布资源包中选一张图片资产，复制进当前项目 workspace，登记为项目
  image asset，并把该分镜 ``keyframes.first`` 指向它。不提交生成、不采用候选。

本模块复用 ``backend.resource_packs`` 的系列/资源包路径校验：资源包只从当前
用户自己的系列目录读取；发表状态和系列归属由已发布包清单保证；媒体文件只
通过 ``resource_packs.asset_file`` 解析，绝不接受任意服务器路径。
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import resource_packs
from .private_auth import current_principal
from .user_context import AccessDenied, UserContext, directory_label
from .workbench_config import MATERIAL_EXTENSIONS

SCHEMA_VERSION = 1
MAX_CHARACTER_PACKS = 8
MAX_BOUND_ASSETS = 64
MAX_PACK_ID = 128
MAX_SEGMENT_ID = 160
COPY_CHUNK_BYTES = 1024 * 1024
FRAMES_DIRECTORY = 'resource-frames'


class ResourceBindingError(RuntimeError):
    """A structured binding/first-frame contract failure."""

    def __init__(self, code: str, message: str, status_code: int = 422, *,
                 blockers: list[dict[str, Any]] | None = None, **details: Any):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.blockers = list(blockers or [])
        self.details = details

    @property
    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'code': self.code,
            'message': str(self),
            'retryable': self.status_code in {409, 502, 503},
            **self.details,
        }
        if self.blockers:
            payload['blockers'] = self.blockers
        return payload


class ResourceBindingRequest(BaseModel):
    """Explicit pack selection for one shot; omitted optional packs are unbound."""

    model_config = ConfigDict(extra='forbid')
    character_pack_ids: list[str] = Field(default_factory=list, max_length=MAX_CHARACTER_PACKS)
    scene_pack_id: str | None = Field(default=None, max_length=MAX_PACK_ID)
    sound_pack_id: str | None = Field(default=None, max_length=MAX_PACK_ID)
    asset_ids: list[str] = Field(default_factory=list, max_length=MAX_BOUND_ASSETS)


class UseResourceFrameRequest(BaseModel):
    """One explicit published-pack image asset to use as the shot first frame."""

    model_config = ConfigDict(extra='forbid')
    pack_id: str = Field(min_length=1, max_length=MAX_PACK_ID)
    asset_id: str = Field(min_length=1, max_length=128)


def _backend():
    # Imported lazily so this module never participates in app.py's import cycle.
    from . import app as backend
    return backend


def _require_context() -> tuple[Any, UserContext]:
    backend = _backend()
    if backend.PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    principal = current_principal.get()
    if principal is None:
        raise HTTPException(401, '请登录工作台')
    return backend, UserContext(principal.identity.username, backend.PRIVATE_WORKSPACES.layout)


def _clean_id(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResourceBindingError('invalid_identifier', f'{label} 必须是文本。')
    text = value.strip()
    if not text:
        raise ResourceBindingError('invalid_identifier', f'{label} 不能为空。')
    if len(text) > maximum:
        raise ResourceBindingError('invalid_identifier', f'{label} 不能超过 {maximum} 个字符。')
    if any(char in text for char in '/\\\x00') or any(ord(char) < 32 for char in text):
        raise ResourceBindingError('invalid_identifier', f'{label} 包含非法字符。')
    return text


def _series_label(manifest: dict[str, Any]) -> str:
    raw = str(manifest.get('series') or '').strip()
    if not raw:
        raise ResourceBindingError('series_required', '作品没有系列登记，无法绑定系列资源包。', 409)
    return directory_label(raw, '未命名系列')


def _project(project_id: str) -> tuple[Any, dict[str, Any], dict[str, Any], Path, str]:
    backend = _backend()
    try:
        manifest = backend.require_project_manifest(project_id)
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResourceBindingError('project_not_found', '作品不存在或无权访问。', 404) from exc
    try:
        document, plan_path = backend.load_project_plan(project_id)
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResourceBindingError('plan_unavailable', f'项目计划不可读取: {exc}', 409) from exc
    return backend, manifest, document, plan_path, _series_label(manifest)


def _segment(document: dict[str, Any], segment_id: str) -> dict[str, Any]:
    segment = next((item for item in document.get('segments', [])
                    if isinstance(item, dict) and str(item.get('id')) == segment_id), None)
    if segment is None:
        raise ResourceBindingError('segment_not_found', f'该作品中不存在此分镜: {segment_id}', 404)
    return segment


def _series_packs(series_label: str) -> tuple[list[dict[str, Any]], list[str]]:
    """All readable packs in the caller's own series directory."""
    try:
        listing = resource_packs.list_packs(series_label)
    except HTTPException as exc:
        if exc.status_code == 404:
            return [], []
        raise
    packs = listing.get('packs') if isinstance(listing.get('packs'), list) else []
    return [item for item in packs if isinstance(item, dict)], listing.get('unreadable') or []


def _published_pack(series_label: str, pack_id: str, *, expected_kind: str | None = None) -> dict[str, Any]:
    clean_id = _clean_id(pack_id, '资源包 ID', MAX_PACK_ID)
    packs, _ = _series_packs(series_label)
    pack = next((item for item in packs if str(item.get('id')) == clean_id), None)
    if pack is None:
        raise ResourceBindingError(
            'resource_pack_not_found',
            '资源包不存在、不属于该作品系列或当前账号不可见；没有跨系列/跨账号回退。',
            404,
            blockers=[{'code': 'resource_pack_not_found', 'message': '同系列已发布资源包中找不到该 ID。',
                       'object': {'kind': 'resource_pack', 'id': clean_id}}],
        )
    if pack.get('status') != 'published':
        raise ResourceBindingError(
            'resource_pack_not_published',
            '资源包尚未发布；草稿/待审资源包不能绑定或用于首帧。',
            409,
            blockers=[{'code': 'resource_pack_not_published',
                       'message': f"资源包 {clean_id} 当前状态为 {pack.get('status')}。",
                       'object': {'kind': 'resource_pack', 'id': clean_id}}],
        )
    if expected_kind is not None and pack.get('kind') != expected_kind:
        raise ResourceBindingError(
            'resource_pack_kind_mismatch',
            f'资源包 {clean_id} 的类型是 {pack.get("kind")}，不是 {expected_kind}。',
            details={'pack_id': clean_id, 'expected_kind': expected_kind, 'actual_kind': pack.get('kind')},
        )
    return pack


def _pack_asset_ids(pack: dict[str, Any]) -> list[str]:
    return [str(item.get('asset_id')) for item in pack.get('assets') or []
            if isinstance(item, dict) and isinstance(item.get('asset_id'), str) and item['asset_id']]


def _resolve_selection(series_label: str, request: ResourceBindingRequest) -> dict[str, Any]:
    character_ids = [_clean_id(value, '角色资源包 ID', MAX_PACK_ID) for value in request.character_pack_ids]
    if len(character_ids) != len(set(character_ids)):
        raise ResourceBindingError('duplicate_pack_id', 'character_pack_ids 不得重复。')
    scene_id = _clean_id(request.scene_pack_id, '场景资源包 ID', MAX_PACK_ID) if request.scene_pack_id is not None else None
    sound_id = _clean_id(request.sound_pack_id, '声音资源包 ID', MAX_PACK_ID) if request.sound_pack_id is not None else None
    selected_ids = [*character_ids, *([scene_id] if scene_id else []), *([sound_id] if sound_id else [])]
    if len(selected_ids) != len(set(selected_ids)):
        raise ResourceBindingError('duplicate_pack_id', '同一个资源包不能绑定到多个字段。')

    character_packs = [_published_pack(series_label, pack_id, expected_kind='character') for pack_id in character_ids]
    scene_pack = _published_pack(series_label, scene_id, expected_kind='scene') if scene_id else None
    sound_pack = _published_pack(series_label, sound_id, expected_kind='sound') if sound_id else None
    packs = [*character_packs, *([scene_pack] if scene_pack else []), *([sound_pack] if sound_pack else [])]

    asset_ids: list[str] = []
    for value in request.asset_ids:
        clean = _clean_id(value, '资源资产 ID', 128)
        if clean not in asset_ids:
            asset_ids.append(clean)
    if asset_ids and not packs:
        raise ResourceBindingError('resource_pack_required', '选择 asset_ids 前必须显式绑定资源包。')
    known_assets = {asset_id: pack for pack in packs for asset_id in _pack_asset_ids(pack)}
    unknown = [asset_id for asset_id in asset_ids if asset_id not in known_assets]
    if unknown:
        raise ResourceBindingError(
            'asset_not_in_selected_packs',
            'asset_ids 只能选择本次显式绑定资源包中的资产；不会自动加入其他包或忽略未知 ID。',
            details={'unknown_asset_ids': unknown,
                     'selected_pack_ids': [str(pack.get('id')) for pack in packs]},
        )

    pack_records: list[dict[str, Any]] = []
    for pack in packs:
        available_ids = _pack_asset_ids(pack)
        chosen = ([asset_id for asset_id in asset_ids if asset_id in available_ids]
                  if asset_ids else available_ids)
        pack_records.append({'pack_id': str(pack.get('id')), 'kind': pack.get('kind'),
                             'version': int(pack.get('version') or 0), 'asset_ids': chosen})
    return {
        'schema_version': SCHEMA_VERSION,
        'character_pack_ids': character_ids,
        'scene_pack_id': scene_id,
        'sound_pack_id': sound_id,
        'asset_ids': asset_ids,
        'packs': pack_records,
        'bound_at': time.time(),
    }


def _public_resources(stored: Any) -> dict[str, Any]:
    """Echo only the contract fields; malformed stored data never crashes a read."""
    empty = {'schema_version': SCHEMA_VERSION, 'character_pack_ids': [], 'scene_pack_id': None,
             'sound_pack_id': None, 'asset_ids': [], 'packs': [], 'bound_at': None}
    if not isinstance(stored, dict):
        return empty
    packs = []
    for item in stored.get('packs') or []:
        if not isinstance(item, dict):
            continue
        packs.append({
            'pack_id': item.get('pack_id'),
            'kind': item.get('kind'),
            'version': item.get('version'),
            'asset_ids': [value for value in item.get('asset_ids') or [] if isinstance(value, str)],
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'character_pack_ids': [value for value in stored.get('character_pack_ids') or [] if isinstance(value, str)],
        'scene_pack_id': stored.get('scene_pack_id') if isinstance(stored.get('scene_pack_id'), str) else None,
        'sound_pack_id': stored.get('sound_pack_id') if isinstance(stored.get('sound_pack_id'), str) else None,
        'asset_ids': [value for value in stored.get('asset_ids') or [] if isinstance(value, str)],
        'packs': packs,
        'bound_at': stored.get('bound_at') if isinstance(stored.get('bound_at'), (int, float)) else None,
    }


def _pack_summary(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': pack.get('id'),
        'kind': pack.get('kind'),
        'title': pack.get('title'),
        'version': pack.get('version'),
        'asset_count': len(pack.get('assets') or []),
        'assets': [{'asset_id': asset.get('asset_id'), 'role': asset.get('role'),
                    'view': asset.get('view'), 'filename': asset.get('filename')}
                   for asset in pack.get('assets') or [] if isinstance(asset, dict)],
    }


def _available_packs(series_label: str) -> list[dict[str, Any]]:
    packs, _ = _series_packs(series_label)
    return [_pack_summary(pack) for pack in packs if pack.get('status') == 'published']


def read_resources(project_id: str, segment_id: str) -> dict[str, Any]:
    """GET: echo the explicit binding plus published-pack facts for this series."""
    backend, manifest, document, _plan_path, _series = _project(project_id)
    segment = _segment(document, segment_id)
    resources = _public_resources(segment.get('resources'))
    series_label = _series_label(manifest)
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'segment_id': segment_id,
        'plan_revision': document.get('revision', 0),
        'resources': resources,
        'bound': bool(resources['packs']),
        'available_packs': _available_packs(series_label),
        'generation_submitted': False,
        'note': '只读回显：资源包绑定不会自动生成、不会自动设置首帧或采用候选。',
    }


def bind_resources(project_id: str, segment_id: str, request: ResourceBindingRequest) -> dict[str, Any]:
    """PUT: validate explicit same-series published packs and write segment.resources."""
    backend, _manifest, document, plan_path, series_label = _project(project_id)
    segment = _segment(document, segment_id)
    resources = _resolve_selection(series_label, request)
    previous = segment.get('resources')
    segment['resources'] = resources
    backend.persist_plan_edit(project_id, document, plan_path)
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'segment_id': segment_id,
        'plan_revision': document.get('revision', 0),
        'resources': _public_resources(resources),
        'bound': bool(resources['packs']),
        'previous_resources': previous if isinstance(previous, dict) else None,
        'generation_submitted': False,
        'note': '已显式写入资源包绑定；未提交生成，未自动使用首帧，也未修改采用状态。',
    }


def _workspace(backend: Any, manifest: dict[str, Any]) -> Path:
    value = manifest.get('workspace_root')
    if not value:
        raise ResourceBindingError('workspace_unavailable', '作品缺少登记的 workspace_root。', 409)
    try:
        workspace = backend.resolve_registered_path(str(value))
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResourceBindingError('workspace_unavailable', f'作品 workspace 不可用: {exc}', 409) from exc
    if not workspace.is_dir():
        raise ResourceBindingError('workspace_unavailable', '作品 workspace 不存在。', 409)
    return workspace.resolve()


def _safe_component(value: str) -> str:
    return ''.join(char if char.isalnum() or char in '-_' else '_' for char in value)[:80] or 'segment'


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _frame_descriptor(project_id: str, segment_id: str, pack: dict[str, Any],
                      asset: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256('\0'.join((
        project_id, segment_id, str(pack.get('id')), str(pack.get('version')),
        str(asset.get('asset_id')), str(asset.get('sha256')), str(asset.get('size')),
    )).encode('utf-8')).hexdigest()[:20]
    suffix = Path(str(asset.get('filename') or '')).suffix.lower()
    return {
        'digest': digest,
        'asset_id': 'RF-' + digest[:12],
        'directory': _safe_component(segment_id)[:32],
        'filename': f'rf-{digest}{suffix}',
    }


def _copy_frame(source: Path, destination: Path, expected_sha256: str) -> tuple[int, str]:
    """Copy one pack image into the project workspace with identity checks."""
    try:
        before = source.stat()
    except OSError as exc:
        raise ResourceBindingError('resource_asset_unavailable', f'资源包图片不可读: {exc}', 409) from exc
    if before.st_size <= 0:
        raise ResourceBindingError('resource_asset_empty', '资源包图片为空，不能作为首帧。', 422)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ResourceBindingError('frame_write_failed', f'无法创建首帧目录: {exc}', 500) from exc
    # Keep sibling/temporary names short: Windows path limits apply to the whole
    # workspace path, and a long segment/asset name must not break a valid copy.
    temporary = destination.with_name(f'.rf-{uuid.uuid4().hex[:16]}.tmp')
    digest = hashlib.sha256()
    written = 0
    try:
        with source.open('rb') as incoming, temporary.open('xb') as outgoing:
            while chunk := incoming.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
                outgoing.write(chunk)
                written += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ResourceBindingError('resource_asset_changed',
                                      '资源包图片在复制期间发生变化，本次首帧未登记。', 409)
        if written != after.st_size:
            raise ResourceBindingError('resource_asset_changed',
                                      '资源包图片复制结果大小不一致，本次首帧未登记。', 409)
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise ResourceBindingError('resource_asset_corrupt',
                                      '资源包图片内容与已发布摘要不一致，本次首帧未登记。', 409)
        os.replace(temporary, destination)
        return written, actual
    except ResourceBindingError:
        raise
    except OSError as exc:
        raise ResourceBindingError('frame_write_failed', f'首帧复制失败: {exc}', 500) from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _manifest_asset(manifest: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    return next((item for item in manifest.get('assets') or []
                 if isinstance(item, dict) and str(item.get('id')) == asset_id), None)


def _register_frame_asset(backend: Any, project_id: str, segment_id: str, pack: dict[str, Any],
                          asset: dict[str, Any], descriptor: dict[str, Any], frame_ref: str,
                          *, previous_first_frame: str | None) -> dict[str, Any]:
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(project_id)
        assets = list(manifest.get('assets') or [])
        record = next((item for item in assets
                       if isinstance(item, dict) and str(item.get('id')) == descriptor['asset_id']), None)
        payload = {
            'id': descriptor['asset_id'],
            'kind': 'image',
            'title': f"{segment_id} · 资源包首帧（{pack.get('title') or pack.get('id')}）",
            'subtitle': '来自已发布资源包；未生成、未采用',
            'ready': True,
            'origin': 'resource-pack-frame',
            'image_path': frame_ref,
            'resource_frame': {
                'pack_id': str(pack.get('id')),
                'pack_kind': pack.get('kind'),
                'pack_version': int(pack.get('version') or 0),
                'pack_asset_id': asset.get('asset_id'),
                'pack_asset_sha256': asset.get('sha256'),
                'target_segment_id': segment_id,
                'target_slot': 'first_frame',
                'frame_path': frame_ref,
                'frame_digest': descriptor['digest'],
                'previous_first_frame': previous_first_frame,
            },
        }
        if record is None:
            assets.append(payload)
        else:
            record.update(payload)
        manifest['assets'] = assets
        backend.save_project_manifest(manifest)
        if project_id == backend.CURRENT_PROJECT_ID:
            backend.apply_project_manifest(manifest)
        return payload


def use_resource_frame(project_id: str, segment_id: str,
                       request: UseResourceFrameRequest) -> dict[str, Any]:
    """POST: copy one published-pack image into the project and set keyframes.first."""
    backend, manifest, document, plan_path, series_label = _project(project_id)
    segment = _segment(document, segment_id)
    pack = _published_pack(series_label, request.pack_id)
    asset_id = _clean_id(request.asset_id, '资源资产 ID', 128)
    asset = next((item for item in pack.get('assets') or []
                  if isinstance(item, dict) and str(item.get('asset_id')) == asset_id), None)
    if asset is None:
        raise ResourceBindingError('resource_asset_not_found', '已发布资源包中不存在此资产。', 404)
    try:
        source = resource_packs.asset_file(series_label, str(pack.get('id')), asset_id)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise ResourceBindingError('resource_asset_unavailable', f'资源包资产不可读取: {exc}', 409) from exc
    suffix = source.suffix.lower()
    if suffix not in MATERIAL_EXTENSIONS['image']:
        raise ResourceBindingError(
            'resource_asset_not_image',
            'use-resource-frame 只接受已发布资源包中的图片资产。',
            details={'asset_id': asset_id, 'suffix': suffix},
        )
    workspace = _workspace(backend, manifest)
    descriptor = _frame_descriptor(project_id, segment_id, pack, asset)
    frame_path = workspace / 'assets' / FRAMES_DIRECTORY / descriptor['directory'] / descriptor['filename']
    if os.name == 'nt' and len(str(frame_path)) > 240:
        raise ResourceBindingError(
            'frame_path_too_long',
            '作品 workspace 路径过长，无法安全保存资源包首帧；请缩短作品目录后重试。',
            409,
            blockers=[{'code': 'frame_path_too_long', 'message': 'Windows 路径长度超过受控上限。',
                       'object': {'kind': 'segment', 'id': segment_id}}],
        )
    frame_ref = backend.root_relative_path(frame_path)
    frames = segment.get('keyframes') if isinstance(segment.get('keyframes'), dict) else {}
    previous = frames.get('first')
    existing_asset = _manifest_asset(manifest, descriptor['asset_id'])
    already_current = (frame_ref == previous and _is_nonempty_file(frame_path)
                       and existing_asset is not None
                       and existing_asset.get('image_path') == frame_ref)
    if already_current:
        continuity = segment.get('resource_frame') if isinstance(segment.get('resource_frame'), dict) else {}
        return _frame_response(project_id, segment_id, document, pack, asset, descriptor,
                               frame_ref, continuity.get('previous_first_frame', previous),
                               created=False, already_current=True)

    size, digest = _copy_frame(source, frame_path, str(asset.get('sha256') or ''))
    registered = _register_frame_asset(backend, project_id, segment_id, pack, asset,
                                       descriptor, frame_ref, previous_first_frame=previous)
    frames = dict(frames)
    frames['first'] = frame_ref
    segment['keyframes'] = frames
    segment['resource_frame'] = {
        'pack_id': str(pack.get('id')),
        'pack_version': int(pack.get('version') or 0),
        'pack_asset_id': asset.get('asset_id'),
        'frame_asset_id': descriptor['asset_id'],
        'frame_path': frame_ref,
        'frame_digest': descriptor['digest'],
        'previous_first_frame': previous,
        'created_at': time.time(),
        'method': 'resource_pack_image_copy',
    }
    backend.persist_plan_edit(project_id, document, plan_path)
    backend.record_segment_artifacts(project_id, segment, 'resource-bindings')
    backend.mark_segment_checkpoints_stale(
        project_id, segment_id, '首帧已由已发布资源包图片设置，需要重新确认受影响阶段')
    response = _frame_response(project_id, segment_id, document, pack, asset, descriptor,
                               frame_ref, previous, created=True, already_current=False)
    response['frame']['size_bytes'] = size
    response['frame']['sha256'] = digest
    response['asset'] = {'id': registered['id'], 'kind': registered['kind'],
                         'title': registered['title'], 'image_path': registered['image_path']}
    return response


def _frame_response(project_id: str, segment_id: str, document: dict[str, Any],
                    pack: dict[str, Any], asset: dict[str, Any], descriptor: dict[str, Any],
                    frame_ref: str, previous_first_frame: str | None, *,
                    created: bool, already_current: bool) -> dict[str, Any]:
    backend = _backend()
    media_ref = None
    try:
        from . import media_refs
        media_ref = media_refs.encode_media_ref({'kind': 'asset', 'asset_id': descriptor['asset_id'],
                                                 'slot': 'image'})
    except (ImportError, ValueError):
        media_ref = None
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'segment_id': segment_id,
        'target_slot': 'first_frame',
        'created': created,
        'already_current': already_current,
        'plan_revision': document.get('revision', 0),
        'target_first_frame': frame_ref,
        'previous_first_frame': previous_first_frame,
        'pack': {'pack_id': str(pack.get('id')), 'kind': pack.get('kind'),
                 'version': int(pack.get('version') or 0), 'title': pack.get('title')},
        'resource_asset': {'asset_id': asset.get('asset_id'), 'filename': asset.get('filename'),
                           'sha256': asset.get('sha256'), 'size': asset.get('size')},
        'frame': {'asset_id': descriptor['asset_id'], 'path': frame_ref,
                  'media_ref': media_ref, 'method': 'resource_pack_image_copy'},
        'note': '已把已发布资源包中的图片登记为该分镜首帧；未提交生成，未修改采用状态。',
    }
