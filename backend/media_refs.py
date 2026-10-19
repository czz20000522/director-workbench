"""Project-scoped media references for remote agents.

Remote MCP responses must never expose a server filesystem path.  This module
turns a project-owned media identity into two things:

* a stable, path-free ``media_ref`` such as ``asset:asset-1:A`` or
  ``reference:ref-1:range-abc:frame`` that survives token expiry and can be
  stored in audit records; and
* a short-lived, HMAC-signed download URL that resolves only inside the
  requested project (``/api/projects/{project_id}/media/{token}``).

The token carries no path.  It carries the project id, the stable reference and
an expiry; resolving it loads the project manifest/plan/reference document,
which enforces account ownership, and then resolves the registered file through
the same helper the local UI uses.  A token issued for one project never
resolves another project's file, and a forged/expired token is rejected before
any file is touched.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException

TOKEN_TTL_SECONDS = 900
MIN_TOKEN_TTL_SECONDS = 60
MAX_TOKEN_TTL_SECONDS = 3600
MAX_MEDIA_REFS = 100
MAX_MEDIA_REF_LENGTH = 512
MAX_LIST_LIMIT = 100

_SECRET = secrets.token_bytes(32)

ASSET_SLOTS = {'auto', 'A', 'image'}
SEGMENT_SLOTS = {'video', 'rework', 'finish', 'first', 'last', 'guide', 'delivery', 'reference'}
TASK_FIELDS = {'video', 'output', 'outputs', 'native_output', 'native_outputs',
               'published_outputs', 'published_native_outputs'}
REFERENCE_SLOTS = {'segment', 'frame', 'audio', 'transcript'}
TIMELINE_SLOTS = {'frame'}
REFERENCE_CATEGORIES = {'all', 'assets', 'segments', 'assembly', 'references', 'tasks'}


class MediaRefError(ValueError):
    """The reference cannot be resolved inside the requested project."""


class MediaRefExpired(MediaRefError):
    """The signed download token is past its expiry."""


# ---------------------------------------------------------------- stable refs

def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        raise MediaRefError(f'{label} ID 无效')
    if value in {'.', '..'} or any(char in ':/\\' or char.isspace() or not char.isprintable() for char in value):
        raise MediaRefError(f'{label} ID 无效')
    return value


def _frame_slot(value: str, *, allowed: set[str]) -> tuple[str, int | None]:
    if value in allowed:
        return value, None
    if value.startswith('frame_'):
        index = value[6:]
        if index.isdigit() and int(index) < 1024:
            return 'frame', int(index)
    raise MediaRefError('媒体引用槽位无效')


def encode_media_ref(target: dict[str, Any]) -> str:
    """Encode a target descriptor as a stable, path-free reference string."""
    kind = target.get('kind')
    if kind == 'asset':
        ref = ':'.join(('asset', _safe_id(target.get('asset_id'), '素材'), _asset_slot(target.get('slot'))))
    elif kind == 'segment':
        ref = ':'.join(('segment', _safe_id(target.get('segment_id'), '分镜'),
                        _segment_slot(target.get('slot'))))
    elif kind == 'assembly':
        ref = 'assembly'
    elif kind == 'task':
        index = target.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
            raise MediaRefError('任务媒体序号无效')
        field = target.get('field')
        if field not in TASK_FIELDS:
            raise MediaRefError('任务媒体字段无效')
        ref = ':'.join(('task', _safe_id(target.get('task_id'), '任务'), field, str(index)))
    elif kind == 'reference-source':
        ref = 'reference-source:' + _safe_id(target.get('analysis_id'), '参考拆解')
    elif kind == 'reference':
        slot = str(target.get('slot') or '')
        index = target.get('index')
        if slot not in REFERENCE_SLOTS:
            raise MediaRefError('参考观察槽位无效')
        if index is not None:
            if slot != 'frame' or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
                raise MediaRefError('参考观察帧序号无效')
            encoded_slot = f'frame_{index}'
        else:
            encoded_slot = slot
        ref = ':'.join(('reference', _safe_id(target.get('analysis_id'), '参考拆解'),
                        _safe_id(target.get('observation_id'), '观察'), encoded_slot))
    elif kind == 'reference-timeline':
        slot = str(target.get('slot') or '')
        index = target.get('index')
        if slot not in TIMELINE_SLOTS:
            raise MediaRefError('参考时间段槽位无效')
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
                raise MediaRefError('参考时间段帧序号无效')
            encoded_slot = f'frame_{index}'
        else:
            encoded_slot = slot
        ref = ':'.join(('reference-timeline', _safe_id(target.get('analysis_id'), '参考拆解'),
                        _safe_id(target.get('segment_id'), '时间段'), encoded_slot))
    elif kind == 'reference-artifact':
        index = target.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
            raise MediaRefError('参考工件证据序号无效')
        ref = ':'.join(('reference-artifact', _safe_id(target.get('analysis_id'), '参考拆解'),
                        _safe_id(target.get('artifact_id'), '工件'), str(index)))
    else:
        raise MediaRefError('未知媒体引用类型')
    if len(ref) > MAX_MEDIA_REF_LENGTH:
        raise MediaRefError('媒体引用过长')
    return ref


def decode_media_ref(ref: Any) -> dict[str, Any]:
    """Decode a stable reference and validate its shape before any lookup."""
    if not isinstance(ref, str) or not ref or len(ref) > MAX_MEDIA_REF_LENGTH or ref != ref.strip():
        raise MediaRefError('媒体引用格式无效')
    parts = ref.split(':')
    head = parts[0]
    if head == 'asset' and len(parts) == 3:
        slot = parts[2]
        if slot not in ASSET_SLOTS:
            raise MediaRefError('媒体引用槽位无效')
        return {'kind': 'asset', 'asset_id': _safe_id(parts[1], '素材'), 'slot': slot}
    if head == 'segment' and len(parts) == 3:
        if parts[2] not in SEGMENT_SLOTS:
            raise MediaRefError('媒体引用槽位无效')
        return {'kind': 'segment', 'segment_id': _safe_id(parts[1], '分镜'), 'slot': parts[2]}
    if head == 'assembly' and len(parts) == 1:
        return {'kind': 'assembly'}
    if head == 'task' and len(parts) == 4:
        if parts[2] not in TASK_FIELDS or not parts[3].isdigit():
            raise MediaRefError('任务媒体引用格式无效')
        index = int(parts[3])
        if index >= 1024:
            raise MediaRefError('任务媒体引用格式无效')
        return {'kind': 'task', 'task_id': _safe_id(parts[1], '任务'), 'field': parts[2], 'index': index}
    if head == 'reference-source' and len(parts) == 2:
        return {'kind': 'reference-source', 'analysis_id': _safe_id(parts[1], '参考拆解')}
    if head == 'reference' and len(parts) == 4:
        slot, index = _frame_slot(parts[3], allowed=REFERENCE_SLOTS)
        return {'kind': 'reference', 'analysis_id': _safe_id(parts[1], '参考拆解'),
                'observation_id': _safe_id(parts[2], '观察'), 'slot': slot, 'index': index}
    if head == 'reference-timeline' and len(parts) == 4:
        slot, index = _frame_slot(parts[3], allowed=TIMELINE_SLOTS)
        return {'kind': 'reference-timeline', 'analysis_id': _safe_id(parts[1], '参考拆解'),
                'segment_id': _safe_id(parts[2], '时间段'), 'slot': slot, 'index': index}
    if head == 'reference-artifact' and len(parts) == 4:
        if not parts[3].isdigit() or int(parts[3]) >= 1024:
            raise MediaRefError('参考工件证据引用格式无效')
        return {'kind': 'reference-artifact', 'analysis_id': _safe_id(parts[1], '参考拆解'),
                'artifact_id': _safe_id(parts[2], '工件'), 'index': int(parts[3])}
    raise MediaRefError('媒体引用格式无效')


def _asset_slot(value: Any) -> str:
    slot = 'auto' if value is None else str(value)
    if slot not in ASSET_SLOTS:
        raise MediaRefError('素材媒体槽位无效')
    return slot


def _segment_slot(value: Any) -> str:
    slot = str(value or '')
    if slot not in SEGMENT_SLOTS:
        raise MediaRefError('分镜媒体槽位无效')
    return slot


# ------------------------------------------------------------- signed tokens

def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _backend():
    from . import app as backend
    return backend


def media_url(project_id: str, token: str) -> str:
    from urllib.parse import quote
    return f"/api/projects/{quote(project_id, safe='')}/media/{token}"


def issue(project_id: str, target: dict[str, Any], *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> dict[str, Any]:
    """Issue the stable reference and its short-lived signed download URL."""
    ttl = _ttl(ttl_seconds)
    ref = encode_media_ref(target)
    payload = {'v': 1, 'p': project_id, 'r': ref, 'e': int(time.time()) + ttl}
    body = _b64encode(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    signature = _b64encode(hmac.new(_SECRET, body.encode('ascii'), hashlib.sha256).digest())
    token = body + '.' + signature
    return {'media_ref': ref, 'url': media_url(project_id, token),
            'expires_at': payload['e'], 'ttl_seconds': ttl}


def _ttl(ttl_seconds: Any) -> int:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise MediaRefError('媒体链接有效期无效')
    if not MIN_TOKEN_TTL_SECONDS <= ttl_seconds <= MAX_TOKEN_TTL_SECONDS:
        raise MediaRefError(f'媒体链接有效期必须在 {MIN_TOKEN_TTL_SECONDS}–{MAX_TOKEN_TTL_SECONDS} 秒之间')
    return ttl_seconds


def resolve_token(project_id: str, token: str) -> dict[str, Any]:
    """Verify a signed token and return its stable target (no path)."""
    if not isinstance(token, str) or token.count('.') != 1 or len(token) > 2048:
        raise MediaRefError('媒体链接无效')
    body, signature = token.split('.', 1)
    expected = _b64encode(hmac.new(_SECRET, body.encode('ascii', 'ignore'), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise MediaRefError('媒体链接无效')
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeError):
        raise MediaRefError('媒体链接无效') from None
    if not isinstance(payload, dict) or payload.get('v') != 1 or payload.get('p') != project_id:
        raise MediaRefError('媒体链接不属于当前作品')
    expires_at = payload.get('e')
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise MediaRefError('媒体链接无效')
    if expires_at <= time.time():
        raise MediaRefExpired('媒体链接已过期，请重新读取作品或媒体列表')
    return decode_media_ref(payload.get('r'))


def issue_many(project_id: str, refs: Iterable[str], *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> list[dict[str, Any]]:
    """Issue tokens for a bounded batch; failures stay per item."""
    values = list(refs)
    if not 1 <= len(values) <= MAX_MEDIA_REFS:
        raise MediaRefError(f'每次需要 1–{MAX_MEDIA_REFS} 个媒体引用')
    ttl = _ttl(ttl_seconds)
    results: list[dict[str, Any]] = []
    for ref in values:
        try:
            target = decode_media_ref(ref)
            issued = issue(project_id, target, ttl_seconds=ttl)
            resolved = resolve_target(project_id, target)
            issued.update(kind=resolved['kind'], filename=resolved['filename'],
                          size_bytes=resolved['size_bytes'], content_type=resolved['content_type'],
                          title=resolved.get('title'))
            results.append({'ok': True, **issued})
        except MediaRefError as exc:
            results.append({'ok': False, 'media_ref': ref if isinstance(ref, str) else None,
                            'error': {'code': 'media_ref_unavailable', 'message': str(exc), 'retryable': False}})
    return results


# ----------------------------------------------------------- target resolve

def _manifest(project_id: str) -> dict[str, Any]:
    backend = _backend()
    try:
        return backend.load_project_manifest(project_id)
    except HTTPException:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise MediaRefError('作品不存在或无权访问') from exc


def _plan(project_id: str) -> dict[str, Any]:
    backend = _backend()
    try:
        return backend.load_project_plan(project_id)[0]
    except HTTPException:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise MediaRefError('作品计划不可读取') from exc


def _analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    backend = _backend()
    try:
        return backend.read_reverse_analysis(project_id, analysis_id)[0]
    except HTTPException as exc:
        raise MediaRefError('参考拆解不存在或无权访问') from exc
    except (RuntimeError, ValueError, OSError) as exc:
        raise MediaRefError('参考拆解不存在或无权访问') from exc


def _registered_file(value: Any, label: str) -> Path:
    backend = _backend()
    if not isinstance(value, str) or not value:
        raise MediaRefError(f'{label}未登记媒体文件')
    try:
        path = backend.resolve_registered_path(value)
    except (ValueError, OSError, RuntimeError) as exc:
        raise MediaRefError(f'{label}文件不可访问') from exc
    except HTTPException as exc:
        raise MediaRefError(f'{label}文件不可访问') from exc
    if not path.is_file():
        raise MediaRefError(f'{label}文件不存在')
    return path


def _material_kind(path: Path) -> str:
    backend = _backend()
    kind = backend.material_kind(path)
    return kind if kind in {'image', 'audio', 'video'} else 'file'


def _describe(path: Path, *, title: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return {'path': path, 'kind': _material_kind(path), 'filename': path.name,
            'size_bytes': stat.st_size, 'content_type': content_type,
            'title': title, 'modified_at': stat.st_mtime}


def _asset_value(asset: dict[str, Any], slot: str) -> tuple[Any, str]:
    if asset.get('kind') == 'image' or slot == 'image':
        return asset.get('image_path') or (asset.get('sources') or {}).get('A'), str(asset.get('title') or asset.get('id'))
    if slot == 'A':
        return (asset.get('sources') or {}).get('A'), str(asset.get('title') or asset.get('id'))
    source = (asset.get('sources') or {}).get('A') or asset.get('image_path')
    return source, str(asset.get('title') or asset.get('id'))


def _task_output(task: dict[str, Any], field: str, index: int) -> tuple[str | None, str | None]:
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    value: Any = None
    title: str | None = None
    if field == 'video':
        values = result.get('published_outputs') or []
        if not values and result.get('output'):
            values = [result['output']]
        if not values and result.get('outputs'):
            values = [f"output/{str(item).replace(chr(92), '/').lstrip('/')}" for item in result['outputs']]
        value = values[index] if index < len(values) else None
        title = '生成候选视频'
    elif field in {'output', 'outputs', 'native_output', 'native_outputs'}:
        raw = result.get(field)
        if isinstance(raw, list):
            value = raw[index] if index < len(raw) else None
        elif index == 0:
            value = raw
        title = '媒体处理产物'
    else:
        values = result.get(field)
        if isinstance(values, list):
            value = values[index] if index < len(values) else None
            title = '生成候选' if field.startswith('published') else '任务产物'
    if isinstance(value, dict):
        title = str(value.get('title') or value.get('kind') or title or '任务产物')
        value = value.get('path')
    return value, title


def resolve_target(project_id: str, target: dict[str, Any]) -> dict[str, Any]:
    """Resolve a decoded target to one project-owned file."""
    kind = target.get('kind')
    if kind == 'asset':
        asset = next((item for item in _manifest(project_id).get('assets', [])
                      if isinstance(item, dict) and str(item.get('id')) == target['asset_id']), None)
        if asset is None:
            raise MediaRefError('素材不存在或无权访问')
        value, title = _asset_value(asset, target.get('slot') or 'auto')
        return {**_describe(_registered_file(value, '素材'), title=title), 'asset_id': target['asset_id']}
    if kind == 'segment':
        segment = next((item for item in _plan(project_id).get('segments', [])
                        if isinstance(item, dict) and str(item.get('id')) == target['segment_id']), None)
        if segment is None:
            raise MediaRefError('分镜不存在或无权访问')
        slot = target['slot']
        video = segment.get('video') if isinstance(segment.get('video'), dict) else {}
        keyframes = segment.get('keyframes') if isinstance(segment.get('keyframes'), dict) else {}
        audio = segment.get('audio') if isinstance(segment.get('audio'), dict) else {}
        values = {
            'video': video.get('path'),
            'rework': (video.get('rework_candidate') or {}).get('path'),
            'finish': (video.get('finish_review') or {}).get('path'),
            'first': keyframes.get('first'),
            'last': keyframes.get('last'),
            'guide': audio.get('guide'),
            'delivery': audio.get('delivery_master'),
            'reference': segment.get('reference_frame'),
        }
        labels = {'video': '当前候选视频', 'rework': '重做候选', 'finish': '精细候选',
                  'first': '首帧', 'last': '尾帧', 'guide': '表演音频', 'delivery': '交付音频',
                  'reference': '参考画面'}
        return {**_describe(_registered_file(values[slot], labels[slot]), title=f"{target['segment_id']} · {labels[slot]}"),
                'segment_id': target['segment_id']}
    if kind == 'assembly':
        assembly = _plan(project_id).get('assembly') or {}
        return {**_describe(_registered_file(assembly.get('output'), '成片候选'), title='成片候选')}
    if kind == 'task':
        backend = _backend()
        task = backend.get_task(target['task_id'])
        if not task or task.get('project_id') != project_id:
            raise MediaRefError('任务不存在或不属于当前作品')
        value, title = _task_output(task, target['field'], target['index'])
        return {**_describe(_registered_file(value, '任务结果'), title=title), 'task_id': target['task_id']}
    if kind == 'reference-source':
        document = _analysis(project_id, target['analysis_id'])
        source = document.get('source') if isinstance(document.get('source'), dict) else {}
        return {**_describe(_registered_file(source.get('path'), '参考源素材'),
                            title=str(source.get('original_name') or document.get('title') or '参考源素材')),
                'analysis_id': target['analysis_id']}
    if kind == 'reference':
        document = _analysis(project_id, target['analysis_id'])
        observation = next((item for item in document.get('observations', [])
                            if isinstance(item, dict) and str(item.get('id')) == target['observation_id']), None)
        if observation is None:
            raise MediaRefError('参考观察不存在')
        slot, index = target['slot'], target.get('index')
        if slot == 'frame' and index is not None:
            frames = observation.get('frames') if isinstance(observation.get('frames'), list) else []
            value = frames[index].get('path') if index < len(frames) and isinstance(frames[index], dict) else None
            label = f'采样帧 {index}'
        else:
            value = {'segment': observation.get('segment_path'), 'frame': observation.get('frame_path'),
                     'audio': observation.get('audio_path'), 'transcript': observation.get('transcript_path')}[slot]
            label = {'segment': '明确范围片段', 'frame': '明确范围帧', 'audio': '明确范围音频',
                     'transcript': 'ASR 结果'}[slot]
        return {**_describe(_registered_file(value, label), title=label),
                'analysis_id': target['analysis_id'], 'observation_id': target['observation_id']}
    if kind == 'reference-timeline':
        document = _analysis(project_id, target['analysis_id'])
        segment = next((item for item in document.get('timeline', [])
                        if isinstance(item, dict) and str(item.get('id')) == target['segment_id']), None)
        if segment is None:
            raise MediaRefError('参考时间段不存在')
        slot, index = target['slot'], target.get('index')
        if slot == 'frame' and index is not None:
            frames = segment.get('frames') if isinstance(segment.get('frames'), list) else []
            value = frames[index].get('path') if index < len(frames) and isinstance(frames[index], dict) else None
            label = f'观察帧 {index}'
        else:
            value = segment.get('frame') or segment.get('frame_path')
            label = '观察帧'
        return {**_describe(_registered_file(value, label), title=label),
                'analysis_id': target['analysis_id'], 'segment_id': target['segment_id']}
    if kind == 'reference-artifact':
        document = _analysis(project_id, target['analysis_id'])
        artifact = (document.get('artifacts') or {}).get(target['artifact_id'])
        evidence = artifact.get('evidence') if isinstance(artifact, dict) else None
        value = evidence[target['index']] if isinstance(evidence, list) and target['index'] < len(evidence) else None
        return {**_describe(_registered_file(value, '参考工件证据'), title='参考工件证据'),
                'analysis_id': target['analysis_id'], 'artifact_id': target['artifact_id']}
    raise MediaRefError('未知媒体引用类型')


# --------------------------------------------------------------- discovery

def _reference_targets(project_id: str) -> list[dict[str, Any]]:
    backend = _backend()
    try:
        analyses = backend.list_reverse_analyses(project_id)['analyses']
    except (HTTPException, RuntimeError, ValueError, OSError):
        return []
    targets: list[dict[str, Any]] = []
    for analysis in analyses:
        analysis_id = str(analysis.get('id') or '')
        if not analysis_id:
            continue
        if isinstance(analysis.get('source'), dict) and analysis['source'].get('path'):
            targets.append({'kind': 'reference-source', 'analysis_id': analysis_id})
        for observation in analysis.get('observations', []):
            if not isinstance(observation, dict) or not observation.get('id'):
                continue
            observation_id = str(observation['id'])
            for slot, key in (('segment', 'segment_path'), ('frame', 'frame_path'),
                              ('audio', 'audio_path'), ('transcript', 'transcript_path')):
                if observation.get(key):
                    targets.append({'kind': 'reference', 'analysis_id': analysis_id,
                                    'observation_id': observation_id, 'slot': slot})
            for index, frame in enumerate(observation.get('frames') or []):
                if isinstance(frame, dict) and frame.get('path'):
                    targets.append({'kind': 'reference', 'analysis_id': analysis_id,
                                    'observation_id': observation_id, 'slot': 'frame', 'index': index})
        observation_ids = {str(item.get('id')) for item in analysis.get('observations', []) if isinstance(item, dict)}
        for segment in analysis.get('timeline', []):
            if not isinstance(segment, dict) or not segment.get('id') or str(segment['id']) in observation_ids:
                continue
            segment_id = str(segment['id'])
            if segment.get('frame') or segment.get('frame_path'):
                targets.append({'kind': 'reference-timeline', 'analysis_id': analysis_id,
                                'segment_id': segment_id, 'slot': 'frame'})
            for index, frame in enumerate(segment.get('frames') or []):
                if isinstance(frame, dict) and frame.get('path'):
                    targets.append({'kind': 'reference-timeline', 'analysis_id': analysis_id,
                                    'segment_id': segment_id, 'slot': 'frame', 'index': index})
        for artifact_id, artifact in (analysis.get('artifacts') or {}).items():
            if not isinstance(artifact, dict):
                continue
            for index, _ in enumerate(artifact.get('evidence') or []):
                targets.append({'kind': 'reference-artifact', 'analysis_id': analysis_id,
                                'artifact_id': str(artifact_id), 'index': index})
    return targets


def _task_targets(project_id: str) -> list[dict[str, Any]]:
    backend = _backend()
    targets: list[dict[str, Any]] = []
    try:
        with backend.DB_LOCK, backend.db() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND status = 'succeeded' "
                "ORDER BY updated_at DESC, id DESC LIMIT 50", (project_id,),
            ).fetchall()
    except Exception:  # A missing/legacy database must not hide other media.
        return targets
    for row in rows:
        task = backend.task_row(row)
        result = task.get('result') if isinstance(task.get('result'), dict) else {}
        field_counts = {
            'video': max(1, len(result.get('published_outputs') or []) or (1 if result.get('output') else 0)
                         or len(result.get('outputs') or [])),
            'published_outputs': len(result.get('published_outputs') or []),
            'published_native_outputs': len(result.get('published_native_outputs') or []),
            'outputs': len(result.get('outputs') or []),
            'native_outputs': len(result.get('native_outputs') or []),
        }
        for field, count in field_counts.items():
            for index in range(min(count, 64)):
                targets.append({'kind': 'task', 'task_id': str(task.get('id')), 'field': field, 'index': index})
    return targets


def list_project_media(project_id: str, *, category: str = 'all', kind: str = 'all',
                       limit: int = 50, offset: int = 0,
                       ttl_seconds: int = TOKEN_TTL_SECONDS) -> dict[str, Any]:
    """List project-owned media as stable refs plus short-lived URLs (no paths)."""
    if category not in REFERENCE_CATEGORIES:
        raise MediaRefError('媒体类别无效')
    if kind not in {'all', 'image', 'audio', 'video', 'file'}:
        raise MediaRefError('媒体类型无效')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise MediaRefError('媒体列表分页无效')
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise MediaRefError('媒体列表分页无效')
    ttl = _ttl(ttl_seconds)
    manifest = _manifest(project_id)
    targets: list[dict[str, Any]] = []
    if category in {'all', 'assets'}:
        for asset in manifest.get('assets', []):
            if not isinstance(asset, dict) or not asset.get('id'):
                continue
            if asset.get('kind') == 'image' and asset.get('image_path'):
                targets.append({'kind': 'asset', 'asset_id': str(asset['id']), 'slot': 'image'})
            elif asset.get('kind') in {'audio', 'video'} and (asset.get('sources') or {}).get('A'):
                targets.append({'kind': 'asset', 'asset_id': str(asset['id']), 'slot': 'A'})
    plan = _plan(project_id) if category in {'all', 'segments', 'assembly'} else {}
    if category in {'all', 'segments'}:
        for segment in plan.get('segments', []):
            if not isinstance(segment, dict) or not segment.get('id'):
                continue
            video = segment.get('video') if isinstance(segment.get('video'), dict) else {}
            keyframes = segment.get('keyframes') if isinstance(segment.get('keyframes'), dict) else {}
            audio = segment.get('audio') if isinstance(segment.get('audio'), dict) else {}
            if video.get('path'):
                targets.append({'kind': 'segment', 'segment_id': str(segment['id']), 'slot': 'video'})
            if (video.get('rework_candidate') or {}).get('path'):
                targets.append({'kind': 'segment', 'segment_id': str(segment['id']), 'slot': 'rework'})
            if (video.get('finish_review') or {}).get('path'):
                targets.append({'kind': 'segment', 'segment_id': str(segment['id']), 'slot': 'finish'})
            for slot, key in (('first', 'first'), ('last', 'last')):
                if keyframes.get(key):
                    targets.append({'kind': 'segment', 'segment_id': str(segment['id']), 'slot': slot})
            for slot, key in (('guide', 'guide'), ('delivery', 'delivery_master')):
                if audio.get(key):
                    targets.append({'kind': 'segment', 'segment_id': str(segment['id']), 'slot': slot})
    if category in {'all', 'assembly'} and (plan.get('assembly') or {}).get('output'):
        targets.append({'kind': 'assembly'})
    if category in {'all', 'references'}:
        targets.extend(_reference_targets(project_id))
    if category in {'all', 'tasks'}:
        targets.extend(_task_targets(project_id))
    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for target in targets:
        try:
            ref = encode_media_ref(target)
        except MediaRefError:
            continue
        if ref in seen:
            continue
        seen.add(ref)
        try:
            described = resolve_target(project_id, target)
            issued = issue(project_id, target, ttl_seconds=ttl)
        except MediaRefError:
            continue
        resolved.append({'media_ref': ref, 'url': issued['url'], 'expires_at': issued['expires_at'],
                         'kind': described['kind'], 'filename': described['filename'],
                         'size_bytes': described['size_bytes'], 'content_type': described['content_type'],
                         'title': described.get('title')})
    if kind != 'all':
        resolved = [item for item in resolved if item['kind'] == kind]
    total = len(resolved)
    selected = resolved[offset:offset + limit]
    return {'media': selected, 'total': total, 'next_offset': offset + limit if offset + limit < total else None}
