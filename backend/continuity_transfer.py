"""Explicit cross-shot last-frame transfer.

This module implements one deterministic operation: the caller names an
already-adopted candidate of one shot (or of the adopted final assembly) and a
target shot's first-frame slot. The workbench decodes that candidate's last
frame with the local FFmpeg, stores it as a new project-owned PNG, registers it
as a project asset, and points the target shot's ``keyframes.first`` at it.

It never submits a generation task, never adopts a candidate, never assembles,
and never rewrites a source file. A missing tool or a source that was not
explicitly adopted is reported as a structured blocker instead of being
silently replaced by another source or frame.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import media_operations, media_refs

SCHEMA_VERSION = 1
SOURCE_SLOTS = ('sample', 'finish', 'final')
TARGET_SLOTS = ('first_frame',)
SLOT_LABELS = {'sample': '样片', 'finish': '精细候选', 'final': '成片'}
EXTRACTION_TIMEOUT_SECONDS = 120

# Module-level so tests and deployments can point at the controlled local tool
# without exposing a free-form command or searching the host PATH.
FFMPEG = media_operations.FFMPEG


class ContinuityError(RuntimeError):
    """A validation or tool failure the HTTP layer can turn into a 4xx/5xx."""

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


class CarryFrameRequest(BaseModel):
    """One explicit source-to-target frame transfer; nothing is inferred."""

    model_config = ConfigDict(extra='forbid')
    source_segment_id: str = Field(min_length=1, max_length=160)
    source_slot: Literal['sample', 'finish', 'final']
    target_slot: Literal['first_frame']


def _backend():
    # Imported lazily so this module never participates in app.py's import cycle.
    from . import app as backend
    return backend


def _segment(document: dict[str, Any], segment_id: str) -> dict[str, Any] | None:
    return next((item for item in document.get('segments', [])
                 if isinstance(item, dict) and str(item.get('id')) == segment_id), None)


def _safe_component(value: str) -> str:
    return ''.join(char if char.isalnum() or char in '-_' else '_' for char in value)[:80] or 'segment'


def _review_for(state: dict[str, Any], asset_id: str, slot: str) -> dict[str, Any] | None:
    reviews = state.get('reviews') if isinstance(state.get('reviews'), dict) else {}
    rows = reviews.get(asset_id) if isinstance(reviews.get(asset_id), dict) else {}
    review = rows.get(slot)
    return review if isinstance(review, dict) else None


def _require_explicit_adoption(review: dict[str, Any] | None, source_id: str, slot: str) -> dict[str, Any]:
    label = SLOT_LABELS.get(slot, slot)
    if review is None or review.get('status') != 'approved':
        raise ContinuityError(
            'source_not_adopted',
            f'{source_id} 的{label}尚未明确采用；只能承接已明确采用的候选。',
            409,
        )
    if review.get('source') != 'explicit-adoption' or not isinstance(review.get('adoption_binding'), dict):
        raise ContinuityError(
            'explicit_adoption_required',
            f'{source_id} 的{label}不是带候选身份和文件身份绑定的明确采用记录。',
            409,
        )
    return review


def _source_slot_path(document: dict[str, Any], source_id: str, slot: str,
                      assembly_id: str) -> tuple[Any, str]:
    """Return the registered slot path plus a human label without resolving it."""
    if slot == 'final':
        if source_id != assembly_id:
            raise ContinuityError(
                'assembly_source_mismatch',
                f'成片槽位只能使用作品成片 {assembly_id}，不能把普通分镜当作成片。',
                422,
            )
        return (document.get('assembly') or {}).get('output'), '成片'
    segment = _segment(document, source_id)
    if segment is None:
        raise ContinuityError('source_segment_not_found', f'来源分镜不存在: {source_id}', 404)
    video = segment.get('video') if isinstance(segment.get('video'), dict) else {}
    if slot == 'sample':
        return video.get('path'), '样片'
    return (video.get('finish_review') or {}).get('path'), '精细候选'


def _media_facts(backend: Any, path: Path) -> dict[str, Any]:
    try:
        facts = backend.authorized_media_facts(str(path))
    except (OSError, ValueError, RuntimeError):
        facts = {'known': False, 'reason': 'media_unavailable'}
    if not isinstance(facts, dict) or facts.get('known') is not True:
        raise ContinuityError('source_unreadable', '无法读取来源视频的媒体事实。', 409)
    return facts


def _last_frame_seconds(facts: dict[str, Any]) -> float:
    duration = facts.get('duration_seconds')
    fps = facts.get('fps')
    if duration is None and facts.get('frames') is not None and fps:
        duration = float(facts['frames']) / float(fps)
    if duration is None:
        raise ContinuityError('source_unreadable', '来源视频缺少可读取的时长，无法定位尾帧。', 409)
    step = (1.0 / float(fps)) if fps else 0.0
    return round(max(0.0, float(duration) - step), 3)


def _resolve_source(project_id: str, source_id: str, slot: str) -> dict[str, Any]:
    """Resolve one explicitly adopted source candidate and its real file."""
    if slot not in SOURCE_SLOTS:
        raise ContinuityError('unknown_source_slot', f'来源阶段必须是 {", ".join(SOURCE_SLOTS)}。', 422)
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    state = backend.build_project_state(project_id)
    assembly_id = str(manifest.get('assembly_asset_id') or 'MASTER')
    # Resolve the declared source segment/slot before reading adoption state, so
    # an unknown shot is a 404 rather than a misleading "not adopted" blocker.
    slot_path, label = _source_slot_path(document, source_id, slot, assembly_id)
    review = _review_for(state, source_id, slot)
    if isinstance(review, dict):
        declared_ref = review.get('candidate_ref')
        if isinstance(declared_ref, str) and declared_ref.strip():
            try:
                declared_path = backend.adoption_candidate_path(declared_ref, project_id)
            except (HTTPException, OSError, ValueError, RuntimeError):
                declared_path = None
            if declared_path is not None and not declared_path.is_file():
                # build_project_state may already mark a missing candidate as
                # stale; report the precise missing file rather than the less
                # specific "not adopted" label.
                raise ContinuityError('source_unavailable',
                                      f'采用候选文件不存在: {declared_path}', 409)
    review = _require_explicit_adoption(review, source_id, slot)
    candidate_ref = review.get('candidate_ref')
    if not isinstance(candidate_ref, str) or not candidate_ref.strip():
        raise ContinuityError('source_candidate_missing',
                              f'{source_id} 的{label}缺少明确候选引用。', 409)
    try:
        path = backend.adoption_candidate_path(candidate_ref, project_id)
    except (HTTPException, OSError, ValueError, RuntimeError) as exc:
        raise ContinuityError('source_unavailable', f'采用候选无法解析: {exc}', 409) from exc
    if not path.is_file():
        raise ContinuityError('source_unavailable', f'采用候选文件不存在: {path}', 409)
    if not backend.same_registered_media(str(slot_path or ''), str(path)):
        raise ContinuityError(
            'source_changed',
            '当前采用候选与分镜/成片登记文件不一致；请重新核对并明确采用当前候选。',
            409,
        )
    try:
        stat = path.stat()
    except OSError as exc:
        raise ContinuityError('source_unavailable', f'采用候选不可读: {exc}', 409) from exc
    binding = review.get('adoption_binding') or {}
    identity = binding.get('file_identity') or {}
    if (identity.get('size_bytes') != stat.st_size
            or identity.get('modified_ns') != stat.st_mtime_ns):
        raise ContinuityError(
            'adoption_candidate_changed',
            '采用候选文件在采用后已变化；请重新明确采用当前候选。',
            409,
        )
    if slot != 'final' and binding.get('segment_signature') != backend.adoption_segment_signature(document, source_id):
        raise ContinuityError(
            'adoption_stale',
            '该分镜内容或当前候选在采用后已变化；请重新明确采用当前候选。',
            409,
        )
    facts = _media_facts(backend, path)
    return {
        'manifest': manifest,
        'document': document,
        'source_segment_id': source_id,
        'source_slot': slot,
        'slot_path': slot_path,
        'path': path,
        'candidate_ref': candidate_ref,
        'review': review,
        'facts': facts,
        'file_identity': {'size_bytes': stat.st_size, 'modified_ns': stat.st_mtime_ns},
        'duration_seconds': facts.get('duration_seconds'),
        'frames': facts.get('frames'),
        'fps': facts.get('fps'),
        'width': facts.get('width'),
        'height': facts.get('height'),
        'last_frame_seconds': _last_frame_seconds(facts),
    }


def _target_context(project_id: str, target_segment_id: str) -> dict[str, Any]:
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    document, plan_path = backend.load_project_plan(project_id)
    assembly_id = str(manifest.get('assembly_asset_id') or 'MASTER')
    if target_segment_id == assembly_id:
        raise ContinuityError('target_must_be_segment',
                              '目标首帧只能写入分镜，不能写入成片槽位。', 422)
    segment = _segment(document, target_segment_id)
    if segment is None:
        raise ContinuityError('target_segment_not_found',
                              f'目标分镜不存在: {target_segment_id}', 404)
    return {'manifest': manifest, 'document': document, 'plan_path': plan_path,
            'segment': segment, 'assembly_id': assembly_id}


def _workspace(backend: Any, manifest: dict[str, Any], project_id: str) -> Path:
    value = manifest.get('workspace_root')
    if not value:
        raise ContinuityError('workspace_unavailable', '作品缺少登记的 workspace_root，无法保存承接首帧。', 409)
    try:
        workspace = backend.resolve_registered_path(str(value))
    except (OSError, ValueError, RuntimeError) as exc:
        raise ContinuityError('workspace_unavailable', f'作品 workspace 不可用: {exc}', 409) from exc
    if not workspace.is_dir():
        raise ContinuityError('workspace_unavailable', f'作品 workspace 不存在: {workspace}', 409)
    return workspace


def _frame_descriptor(project_id: str, target_id: str, source: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps({
        'project_id': project_id,
        'target_segment_id': target_id,
        'source_segment_id': source['source_segment_id'],
        'source_slot': source['source_slot'],
        'candidate_ref': source['candidate_ref'],
        **source['file_identity'],
    }, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
    asset_id = 'CF-' + digest[:12]
    return {'digest': digest, 'asset_id': asset_id,
            'directory_name': _safe_component(target_id),
            'filename': f'carry-{digest}.png'}


def _extract_last_frame(source: Path, output: Path) -> dict[str, Any]:
    """Decode the last frame of ``source`` into one new PNG at ``output``."""
    if not FFMPEG.is_file():
        raise ContinuityError(
            'continuity_tool_unavailable',
            '本地 FFmpeg 不可用，无法抽取上一镜尾帧。',
            503,
            blockers=[{'code': 'ffmpeg_missing',
                       'message': f'受控 FFmpeg 不存在或不可执行: {FFMPEG}',
                       'path': str(FFMPEG)}],
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.stat().st_size <= 0:
        try:
            output.unlink()
        except OSError:
            pass
    temporary = output.with_name(f'.{output.stem}-{uuid.uuid4().hex}.tmp.png')
    log_path = output.with_name(f'.{output.stem}-{uuid.uuid4().hex}.ffmpeg.log')
    command = [
        str(FFMPEG), '-nostdin', '-v', 'error', '-y',
        '-sseof', '-0.1', '-i', str(source),
        '-frames:v', '1', '-an', '-sn', '-dn',
        '-f', 'image2', '-c:v', 'png', str(temporary),
    ]
    started = time.monotonic()
    try:
        try:
            # Write the tool log to a file rather than a pipe: isolated test
            # fixtures and some hosts replace reader threads, while a file
            # keeps stdout/stderr bounded and avoids blocking the request.
            with log_path.open('wb') as log:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT)
                try:
                    returncode = process.wait(timeout=EXTRACTION_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                class _Completed:
                    pass
                completed = _Completed()
                completed.returncode = returncode
        except FileNotFoundError as exc:
            raise ContinuityError(
                'continuity_tool_unavailable', '本地 FFmpeg 不存在，无法抽取上一镜尾帧。', 503,
                blockers=[{'code': 'ffmpeg_missing', 'message': str(exc)}],
            ) from exc
        except subprocess.TimeoutExpired as exc:
            diagnostic = log_path.read_text(encoding='utf-8', errors='replace')[-800:] if log_path.is_file() else ''
            raise ContinuityError(
                'frame_extraction_timeout', 'FFmpeg 抽取尾帧超时。', 502,
                blockers=[{'code': 'frame_extraction_timeout', 'message': diagnostic or str(exc)}],
            ) from exc
        diagnostic = log_path.read_text(encoding='utf-8', errors='replace').strip() if log_path.is_file() else ''
        if completed.returncode != 0:
            raise ContinuityError(
                'frame_extraction_failed', 'FFmpeg 未能抽取来源视频尾帧。', 502,
                blockers=[{'code': 'frame_extraction_failed',
                           'message': diagnostic[-800:] or f'exit {completed.returncode}'}],
            )
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ContinuityError(
                'frame_extraction_failed', 'FFmpeg 没有生成非空 PNG。', 502,
                blockers=[{'code': 'frame_output_empty'}],
            )
        with temporary.open('rb') as handle:
            signature = handle.read(8)
        if signature != b'\x89PNG\r\n\x1a\n':
            raise ContinuityError(
                'frame_extraction_failed', 'FFmpeg 输出不是有效 PNG。', 502,
                blockers=[{'code': 'frame_output_not_png'}],
            )
        os.replace(temporary, output)
        return {'output': output, 'elapsed_seconds': round(time.monotonic() - started, 3),
                'command': command}
    finally:
        for path in (temporary, log_path):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


def _valid_png(path: Path) -> bool:
    """True only for an existing non-empty file starting with a PNG signature."""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open('rb') as handle:
            return handle.read(8) == b'\x89PNG\r\n\x1a\n'
    except OSError:
        return False


def _register_asset(backend: Any, project_id: str,
                    target_id: str, source: dict[str, Any], descriptor: dict[str, Any],
                    frame_ref: str) -> dict[str, Any]:
    with backend.MATERIAL_REUSE_LOCK:
        # Re-read the manifest under the same lock used by uploads/reuse so a
        # concurrent material registration is not lost by this asset append.
        manifest = backend.require_project_manifest(project_id)
        assets = list(manifest.get('assets') or [])
        record = next((item for item in assets
                       if isinstance(item, dict) and str(item.get('id')) == descriptor['asset_id']), None)
        payload = {
            'id': descriptor['asset_id'],
            'kind': 'image',
            'title': f'{target_id} · 承接首帧（{source["source_segment_id"]} {source["source_slot"]}）',
            'subtitle': '从上一镜已采用候选抽取尾帧；未生成、未采用',
            'ready': True,
            'origin': 'continuity-carry-frame',
            'image_path': frame_ref,
            'continuity_frame': {
                'source_segment_id': source['source_segment_id'],
                'source_slot': source['source_slot'],
                'source_candidate_ref': source['candidate_ref'],
                'source_file_identity': source['file_identity'],
                'target_segment_id': target_id,
                'target_slot': 'first_frame',
                'frame_path': frame_ref,
                'frame_digest': descriptor['digest'],
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


def _apply_target(backend: Any, project_id: str, document: dict[str, Any], plan_path: Path,
                  target: dict[str, Any], source: dict[str, Any], descriptor: dict[str, Any],
                  frame_ref: str) -> dict[str, Any]:
    frames = dict(target.get('keyframes') or {})
    previous = frames.get('first')
    frames['first'] = frame_ref
    target['keyframes'] = frames
    target['continuity_frame'] = {
        'source_segment_id': source['source_segment_id'],
        'source_slot': source['source_slot'],
        'source_candidate_ref': source['candidate_ref'],
        'source_file_identity': source['file_identity'],
        'target_slot': 'first_frame',
        'frame_path': frame_ref,
        'frame_asset_id': descriptor['asset_id'],
        'frame_digest': descriptor['digest'],
        'previous_first_frame': previous,
        'created_at': time.time(),
        'method': 'ffmpeg_last_frame_png',
    }
    backend.persist_plan_edit(project_id, document, plan_path)
    backend.record_segment_artifacts(project_id, target)
    backend.mark_segment_checkpoints_stale(
        project_id, str(target.get('id')), '首帧已由上一镜尾帧承接，需要重新确认受影响阶段')
    return {'previous_first_frame': previous, 'plan_revision': document.get('revision')}


def _response(project_id: str, target_id: str, source: dict[str, Any],
              descriptor: dict[str, Any], frame_ref: str, frame_path: Path,
              *, created: bool, extracted: bool, already_current: bool,
              previous_first_frame: str | None, plan_revision: Any,
              target_first_frame: str) -> dict[str, Any]:
    backend = _backend()
    media_ref = media_refs.encode_media_ref({'kind': 'asset',
                                             'asset_id': descriptor['asset_id'],
                                             'slot': 'image'})
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'segment_id': target_id,
        'target_slot': 'first_frame',
        'created': created,
        'extracted': extracted,
        'already_current': already_current,
        'plan_revision': plan_revision,
        'target_first_frame': target_first_frame,
        'previous_first_frame': previous_first_frame,
        'source': {
            'segment_id': source['source_segment_id'],
            'slot': source['source_slot'],
            'candidate_ref': source['candidate_ref'],
            'path': backend.root_relative_path(source['path']),
            'review_revision': (source.get('review') or {}).get('revision'),
            'duration_seconds': source.get('duration_seconds'),
            'last_frame_seconds': source.get('last_frame_seconds'),
            'frames': source.get('frames'),
            'fps': source.get('fps'),
            'width': source.get('width'),
            'height': source.get('height'),
        },
        'frame': {
            'asset_id': descriptor['asset_id'],
            'path': frame_ref,
            'media_ref': media_ref,
            'size_bytes': frame_path.stat().st_size if frame_path.is_file() else None,
            'method': 'ffmpeg_last_frame_png',
        },
        'note': '已把上一镜已采用候选的尾帧登记为目标分镜首帧；未提交生成，未修改采用状态。',
    }


def carry_frame(project_id: str, target_segment_id: str, request: CarryFrameRequest) -> dict[str, Any]:
    """Carry one adopted source candidate's last frame into a target first slot."""
    backend = _backend()
    if request.target_slot not in TARGET_SLOTS:
        raise ContinuityError('unknown_target_slot', '目标槽位只支持 first_frame。', 422)
    if request.source_segment_id == target_segment_id:
        raise ContinuityError('source_is_target', '来源分镜不能与目标分镜相同。', 422)
    source = _resolve_source(project_id, request.source_segment_id, request.source_slot)
    target_context = _target_context(project_id, target_segment_id)
    # Both reads must describe the same plan revision before the first write.
    if target_context['document'].get('revision') != source['document'].get('revision'):
        raise ContinuityError('plan_changed', '计划在读取来源后已变化；请重新读取后重试。', 409)
    target = target_context['segment']
    document = target_context['document']
    plan_path = target_context['plan_path']
    descriptor = _frame_descriptor(project_id, target_segment_id, source)
    workspace = _workspace(backend, source['manifest'], project_id)
    frame_path = workspace / 'continuity' / descriptor['directory_name'] / descriptor['filename']
    frame_ref = backend.root_relative_path(frame_path)
    frames = target.get('keyframes') if isinstance(target.get('keyframes'), dict) else {}
    continuity = target.get('continuity_frame') if isinstance(target.get('continuity_frame'), dict) else {}
    already_current = (frames.get('first') == frame_ref and continuity.get('frame_digest') == descriptor['digest']
                       and _valid_png(frame_path))
    if already_current:
        return _response(project_id, target_segment_id, source, descriptor, frame_ref, frame_path,
                         created=False, extracted=False, already_current=True,
                         previous_first_frame=continuity.get('previous_first_frame'),
                         plan_revision=document.get('revision'), target_first_frame=frame_ref)

    try:
        before = source['path'].stat()
    except OSError as exc:
        raise ContinuityError('source_unavailable', f'来源文件不可读: {exc}', 409) from exc
    extracted = False
    if not _valid_png(frame_path):
        _extract_last_frame(source['path'], frame_path)
        extracted = True
    try:
        after = source['path'].stat()
    except OSError as exc:
        raise ContinuityError('source_unavailable', f'来源文件在抽取后不可读: {exc}', 409) from exc
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        try:
            frame_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContinuityError('source_changed',
                              '来源文件在抽取期间发生变化；本次承接未登记。', 409)

    asset = _register_asset(backend, project_id, target_segment_id,
                            source, descriptor, frame_ref)
    applied = _apply_target(backend, project_id, document, plan_path, target, source,
                            descriptor, frame_ref)
    response = _response(project_id, target_segment_id, source, descriptor, frame_ref, frame_path,
                         created=True, extracted=extracted, already_current=False,
                         previous_first_frame=applied['previous_first_frame'],
                         plan_revision=applied['plan_revision'], target_first_frame=frame_ref)
    response['asset'] = {'id': asset['id'], 'kind': asset['kind'], 'title': asset['title'],
                         'image_path': asset['image_path']}
    return response


def capability(project_id: str, target_segment_id: str) -> dict[str, Any]:
    """Return factual source candidates and whether a last-frame carry is possible."""
    backend = _backend()
    target_context = _target_context(project_id, target_segment_id)
    document = target_context['document']
    assembly_id = target_context['assembly_id']
    state = backend.build_project_state(project_id)
    tool_available = FFMPEG.is_file()
    candidates: list[dict[str, Any]] = []

    def add_candidate(source_id: str, slot: str) -> None:
        review = _review_for(state, source_id, slot)
        row: dict[str, Any] = {
            'source_segment_id': source_id,
            'source_slot': slot,
            'label': SLOT_LABELS.get(slot, slot),
            'adopted': bool(review and review.get('status') == 'approved'),
            'explicit_adoption': bool(review and review.get('source') == 'explicit-adoption'
                                      and isinstance(review.get('adoption_binding'), dict)),
            'review_revision': (review or {}).get('revision'),
            'candidate_ref': (review or {}).get('candidate_ref'),
            'available': False,
            'blockers': [],
        }
        if not row['explicit_adoption']:
            return
        try:
            resolved = _resolve_source(project_id, source_id, slot)
        except ContinuityError as exc:
            row['blockers'].append(exc.detail)
            candidates.append(row)
            return
        row.update({
            'available': True,
            'candidate_ref': resolved['candidate_ref'],
            'path': backend.root_relative_path(resolved['path']),
            'review_revision': (resolved.get('review') or {}).get('revision'),
            'duration_seconds': resolved.get('duration_seconds'),
            'last_frame_seconds': resolved.get('last_frame_seconds'),
            'frames': resolved.get('frames'),
            'fps': resolved.get('fps'),
            'width': resolved.get('width'),
            'height': resolved.get('height'),
        })
        candidates.append(row)

    for segment in document.get('segments', []):
        if not isinstance(segment, dict) or str(segment.get('id')) == target_segment_id:
            continue
        for slot in ('sample', 'finish'):
            add_candidate(str(segment.get('id')), slot)
    if assembly_id != target_segment_id:
        add_candidate(assembly_id, 'final')

    available_sources = [row for row in candidates if row['available']]
    blockers: list[dict[str, Any]] = []
    if not tool_available:
        blockers.append({'code': 'ffmpeg_missing',
                         'message': f'受控 FFmpeg 不可用: {FFMPEG}',
                         'path': str(FFMPEG)})
    if not available_sources:
        if candidates:
            blockers.append({'code': 'no_available_source',
                             'message': '已采用源候选存在，但当前文件不可读或采用绑定已失效；请重新核对并明确采用。'})
        else:
            blockers.append({'code': 'no_adopted_source',
                             'message': '没有可承接的已明确采用源候选；请先完成审核并显式采用上一镜候选。'})
    frames = target_context['segment'].get('keyframes') if isinstance(
        target_context['segment'].get('keyframes'), dict) else {}
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'target_segment_id': target_segment_id,
        'target_slot': 'first_frame',
        'plan_revision': document.get('revision'),
        'can_carry': tool_available and bool(available_sources),
        'tool': {
            'name': 'ffmpeg',
            'available': tool_available,
            'method': 'ffmpeg_last_frame_png',
            'missing': [] if tool_available else [str(FFMPEG)],
            'note': '使用受控 FFmpeg 抽取来源视频最后一帧并保存为新 PNG；不提交生成。',
        },
        'target': {
            'segment_id': target_segment_id,
            'current_first_frame': frames.get('first'),
            'has_current_frame': bool(frames.get('first')),
            'status': target_context['segment'].get('status'),
        },
        'source_slots': list(SOURCE_SLOTS),
        'target_slots': list(TARGET_SLOTS),
        'source_candidates': candidates,
        'blockers': blockers,
        'note': '只返回事实与结构化阻断项；不替调用方选择来源或槽位。',
    }
