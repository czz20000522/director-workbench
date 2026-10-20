"""Explicit 2K/4K delivery upscale candidates.

The workbench only renders, records and compares. This module turns one
explicitly selected, explicitly adopted source into one *new* candidate file:

* the source is never modified or used as the output path;
* the candidate is never adopted or assembled automatically;
* the caller names the target resolution, the method, whether audio is kept,
  and which adopted source slot to use;
* a missing tool is reported as a structured blocker instead of being replaced
  with a different method.

``lanczos_only`` runs local FFmpeg synchronously. ``superres_2x+lanczos`` needs
a local super-resolution runner (``DIRECTOR_SUPERRES_COMMAND``) plus the
RealESRGAN model already used by :mod:`backend.enhancement`; while no runner is
configured the method reports ``superres_tool_missing`` and is not silently
downgraded. ``run_delivery_upscale_task`` is the single execution entry used by
the synchronous HTTP route today and by a future queued runner.
"""
from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import paths

from . import enhancement, output_resolution
from .media_operations import FFMPEG

SCHEMA_VERSION = 1
TARGET_RESOLUTIONS = {'2k': 2048, '4k': 3840}
METHODS = ('superres_2x+lanczos', 'lanczos_only')
SOURCE_SLOTS = ('sample', 'finish', 'final')
SLOT_LABELS = {'sample': '样片', 'finish': '精细候选', 'final': '成片'}
# A delivery ladder source above this is not a resumable/upscalable candidate.
MAX_SOURCE_PIXELS = TARGET_RESOLUTIONS['4k'] * (TARGET_RESOLUTIONS['4k'] * 9 // 16)
SUPERRES_ENV = 'DIRECTOR_SUPERRES_COMMAND'
EXECUTION_TIMEOUT_SECONDS = 1800


class UpscaleError(RuntimeError):
    """A structured blocker/validation failure the HTTP layer can expose."""

    def __init__(self, code: str, message: str, status_code: int = 422, **details: Any):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details

    @property
    def detail(self) -> dict[str, Any]:
        return {'code': self.code, 'message': str(self), **self.details}


class DeliveryUpscaleRequest(BaseModel):
    """One explicit delivery-upscale request; nothing here is inferred."""

    source_asset_id: str = Field(min_length=1, max_length=160)
    source_slot: Literal['sample', 'finish', 'final']
    target_resolution: Literal['2k', '4k']
    method: Literal['superres_2x+lanczos', 'lanczos_only']
    preserve_audio: bool
    idempotency_key: str = Field(min_length=1, max_length=160)


def _backend():
    # Imported lazily so this module never participates in app.py's import cycle.
    from . import app as backend
    return backend


def _superres_command() -> str | None:
    value = os.environ.get(SUPERRES_ENV, '').strip()
    return value or None


def _model_path() -> Path:
    return paths.SHARED_ROOT / 'models' / 'upscale_models' / enhancement.MODEL


def _method_blockers(method: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if not FFMPEG.is_file():
        blockers.append({'code': 'ffmpeg_missing',
                         'message': f'本地 FFmpeg 不可用: {FFMPEG}'})
    if method == 'superres_2x+lanczos':
        if not _superres_command():
            blockers.append({'code': 'superres_tool_missing',
                             'message': f'未配置本地超分运行器；设置 {SUPERRES_ENV} 为包含 {{input}} 和 {{output}} 的命令模板后才可用'})
        if not _model_path().is_file():
            blockers.append({'code': 'superres_model_missing',
                             'message': f'缺少超分模型: {_model_path()}'})
    return blockers


def native_detail(method: str) -> bool:
    """Only a pipeline with a real super-resolution step adds model detail."""
    return method == 'superres_2x+lanczos'


def capability() -> dict[str, Any]:
    """Return facts about available methods and input limits; choose nothing."""
    ffmpeg_available = FFMPEG.is_file()
    methods: dict[str, Any] = {
        'lanczos_only': {
            'available': ffmpeg_available,
            'native_detail': False,
            'requires': ['ffmpeg'],
            'blockers': _method_blockers('lanczos_only'),
            'note': '仅用 FFmpeg lanczos 缩放到目标尺寸；不增加模型细节。',
        },
        'superres_2x+lanczos': {
            'available': ffmpeg_available and bool(_superres_command()) and _model_path().is_file(),
            'native_detail': True,
            'requires': ['ffmpeg', SUPERRES_ENV, enhancement.MODEL],
            'model': {'name': enhancement.MODEL, 'present': _model_path().is_file(), 'scale': 2},
            'runner_configured': bool(_superres_command()),
            'blockers': _method_blockers('superres_2x+lanczos'),
            'note': '先由本地超分运行器做 2x 超分，再由 FFmpeg lanczos 缩放到目标；包含超分步骤，但不宣称目标分辨率全部为原生像素。',
        },
    }
    return {
        'schema_version': SCHEMA_VERSION,
        'target_resolutions': {key: {'long_edge': edge} for key, edge in TARGET_RESOLUTIONS.items()},
        'methods': methods,
        'methods_order': list(METHODS),
        'source_slots': list(SOURCE_SLOTS),
        'input_limits': {
            'max_source_pixels': MAX_SOURCE_PIXELS,
            'max_source_long_edge': TARGET_RESOLUTIONS['4k'],
            'target_must_exceed_source': True,
            'note': '源必须小于目标；超过 4K 输入上限或已达到目标长边的源会明确阻塞。4K 是超分/缩放交付候选，不是原生 4K 生成，也不代表画质验收。',
        },
        'preserve_audio': {'required': True,
                           'note': 'true 时复制源音轨；false 时生成无音轨候选。音频状态会写入回执。'},
        'execution': 'local synchronous FFmpeg; no downloads, no automatic adoption or assembly',
    }


def target_dimensions(width: int, height: int, target_resolution: str) -> dict[str, Any]:
    """Return the exact even target size while preserving the source aspect."""
    if target_resolution not in TARGET_RESOLUTIONS:
        raise UpscaleError('unknown_target_resolution', f'目标分辨率必须是 {", ".join(TARGET_RESOLUTIONS)}')
    if not width or not height or width < 1 or height < 1:
        raise UpscaleError('source_unreadable', '源视频缺少可读取的画面尺寸', 409)
    if width * height > MAX_SOURCE_PIXELS:
        raise UpscaleError('source_exceeds_4k_input_limit',
                           f'源视频 {width}×{height} 超过 4K 输入上限 {MAX_SOURCE_PIXELS} 像素', 422,
                           source={'width': width, 'height': height}, max_source_pixels=MAX_SOURCE_PIXELS)
    long_edge = TARGET_RESOLUTIONS[target_resolution]
    if max(width, height) >= long_edge:
        raise UpscaleError('source_not_below_target',
                           f'源视频长边 {max(width, height)} 不小于目标 {target_resolution.upper()} 长边 {long_edge}；无需或不能生成该交付候选', 422,
                           source={'width': width, 'height': height}, target_long_edge=long_edge)
    ratio = width / height
    known_aspect = ('1:1' if width == height else '16:9' if abs(ratio - 16 / 9) < 1e-3
                    else '9:16' if abs(ratio - 9 / 16) < 1e-3 else None)
    if known_aspect:
        long_side, short_side = output_resolution.dimensions(target_resolution, known_aspect)
        return {'resolution': target_resolution, 'width': long_side, 'height': short_side,
                'long_edge': long_edge, 'aspect_preserved': True, 'source_aspect': known_aspect}
    scale = long_edge / max(width, height)
    target_width = max(2, int(round(width * scale / 2)) * 2)
    target_height = max(2, int(round(height * scale / 2)) * 2)
    return {'resolution': target_resolution, 'width': target_width, 'height': target_height,
            'long_edge': long_edge, 'aspect_preserved': False, 'source_aspect': f'{width}:{height}'}


def _adopted_review(project_id: str, source_asset_id: str, source_slot: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    state = backend.build_project_state(project_id)
    if source_slot not in SOURCE_SLOTS:
        raise UpscaleError('unknown_source_slot', f'来源阶段必须是 {", ".join(SOURCE_SLOTS)}')
    assembly_id = str(manifest.get('assembly_asset_id') or 'MASTER')
    reviews = (state.get('reviews') or {}).get(source_asset_id) or {}
    review = reviews.get(source_slot)
    label = SLOT_LABELS[source_slot]
    if not isinstance(review, dict) or review.get('status') != 'approved':
        raise UpscaleError('source_not_adopted',
                           f'{source_asset_id} 的{label}尚未明确采用；只能对已采用/成片源生成交付候选', 409)
    if review.get('source') != 'explicit-adoption' or not isinstance(review.get('adoption_binding'), dict):
        raise UpscaleError('explicit_adoption_required',
                           f'{source_asset_id} 的{label}不是带候选身份的明确采用记录', 409)
    binding = review['adoption_binding']
    if source_slot == 'final':
        if source_asset_id != assembly_id:
            raise UpscaleError('assembly_source_mismatch', f'成片来源必须是 {assembly_id}', 422)
        slot_path = (document.get('assembly') or {}).get('output')
    else:
        segment = next((item for item in document.get('segments', [])
                        if isinstance(item, dict) and str(item.get('id')) == source_asset_id), None)
        if segment is None:
            raise UpscaleError('segment_not_found', f'分镜不存在: {source_asset_id}', 404)
        video = segment.get('video') or {}
        slot_path = video.get('path') if source_slot == 'sample' else (video.get('finish_review') or {}).get('path')
    candidate_ref = review.get('candidate_ref')
    if not isinstance(candidate_ref, str) or not candidate_ref:
        raise UpscaleError('source_candidate_missing', f'{source_asset_id} 的{label}缺少明确候选引用', 409)
    try:
        path = backend.adoption_candidate_path(candidate_ref, project_id)
    except HTTPException as exc:
        raise UpscaleError('source_unavailable', str(exc.detail), 409) from exc
    if not path.is_file():
        raise UpscaleError('source_unavailable', f'采用候选文件不存在: {path}', 409)
    if not backend.same_registered_media(str(slot_path or ''), str(path)):
        raise UpscaleError('source_changed',
                           '当前采用候选与分镜/成片记录不一致；请重新核对并明确采用当前候选', 409)
    try:
        identity = path.stat()
    except OSError as exc:
        raise UpscaleError('source_unavailable', f'采用候选不可读: {exc}', 409) from exc
    file_identity = binding.get('file_identity') or {}
    if (file_identity.get('size_bytes') != identity.st_size
            or file_identity.get('modified_ns') != identity.st_mtime_ns):
        raise UpscaleError('adoption_candidate_changed',
                           '采用候选文件已变化；请重新采用当前候选后再生成交付候选', 409)
    if source_slot != 'final' and binding.get('segment_signature') != backend.adoption_segment_signature(document, source_asset_id):
        raise UpscaleError('adoption_stale',
                           '该分镜内容或当前候选在采用后已变化；请重新明确采用后再生成交付候选', 409)
    facts = backend.authorized_media_facts(str(path))
    if not facts.get('known') or not facts.get('width') or not facts.get('height') or not facts.get('duration_seconds'):
        raise UpscaleError('source_unreadable', '无法读取源视频尺寸或时长', 409)
    return manifest, document, {'path': path, 'slot': source_slot, 'asset_id': source_asset_id,
                                'candidate_ref': candidate_ref, 'review': review, 'facts': facts}


def _source_record(source: dict[str, Any]) -> dict[str, Any]:
    backend = _backend()
    path: Path = source['path']
    stat = path.stat()
    facts = source['facts']
    review = source['review']
    return {
        'asset_id': source['asset_id'], 'slot': source['slot'],
        'path': backend.root_relative_path(path), 'candidate_ref': source['candidate_ref'],
        'review_revision': review.get('revision'),
        'width': facts.get('width'), 'height': facts.get('height'),
        'duration_seconds': facts.get('duration_seconds'), 'fps': facts.get('fps'),
        'frames': facts.get('frames'), 'audio_streams': facts.get('audio_streams'),
        'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
    }


def _prepare_payload(project_id: str, manifest: dict[str, Any], request: DeliveryUpscaleRequest,
                     source: dict[str, Any], task_id: str) -> dict[str, Any]:
    if request.method not in METHODS:
        raise UpscaleError('unknown_method', f'方法必须是 {", ".join(METHODS)}')
    target = target_dimensions(int(source['facts']['width']), int(source['facts']['height']),
                               request.target_resolution)
    return {
        'kind': 'delivery_upscale',
        'asset_id': f'UP-{task_id[:8]}',
        'project_id': project_id,
        'workspace_root': str(manifest['workspace_root']),
        'source': _source_record(source),
        'target': target,
        'method': request.method,
        'native_detail': native_detail(request.method),
        'preserve_audio': request.preserve_audio,
        'request': request.model_dump(mode='json'),
        'submission_receipt': {'key': request.idempotency_key,
                               'request': request.model_dump(mode='json')},
    }


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _run(command: list[str], *, log_path: Path, stopped: Callable[[], bool],
         timeout: int = EXECUTION_TIMEOUT_SECONDS) -> None:
    """Run one bounded tool invocation; stop kills only this owned process."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open('wb') as output:
        process = subprocess.Popen(command, stdout=output, stderr=output,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                if stopped():
                    raise UpscaleError('upscale_stopped', '交付超分已停止；源文件保留', 409)
                if time.monotonic() - started > timeout:
                    raise UpscaleError('upscale_timeout', '交付超分超时；源文件保留，请核对后重试', 504)
                time.sleep(.2)
        finally:
            _terminate(process)
    if process.returncode:
        tail = ''
        try:
            tail = log_path.read_text(encoding='utf-8', errors='replace')[-1200:].strip()
        except OSError:
            pass
        raise UpscaleError('upscale_tool_failed', tail or 'FFmpeg/超分命令执行失败')


def _ffmpeg_command(source: Path, output: Path, size: dict[str, Any], *,
                    preserve_audio: bool, audio_streams: int, second_input: Path | None = None) -> list[str]:
    video_map = '0:v:0'
    command = [str(FFMPEG), '-nostdin', '-n', '-hide_banner', '-loglevel', 'error',
               '-threads', '2', '-i', str(source)]
    if second_input is not None:
        command += ['-i', str(second_input)]
        audio_map = '1:a?'
    else:
        audio_map = '0:a?'
    command += ['-map', video_map,
                '-vf', f'scale={size["width"]}:{size["height"]}:flags=lanczos,setsar=1',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-threads', '2',
                '-pix_fmt', 'yuv420p']
    if preserve_audio and audio_streams:
        command += ['-map', audio_map, '-c:a', 'aac', '-b:a', '192k']
    else:
        command += ['-an']
    if second_input is not None:
        command += ['-shortest']
    command += ['-movflags', '+faststart', str(output)]
    return command


def _superres_argv(source: Path, output: Path) -> list[str]:
    template = _superres_command()
    if not template:
        raise UpscaleError('superres_tool_missing',
                           f'未配置本地超分运行器；设置 {SUPERRES_ENV} 后才可用', 503,
                           blockers=[{'code': 'superres_tool_missing', 'message': f'缺少 {SUPERRES_ENV}'}])
    if '{input}' not in template or '{output}' not in template:
        raise UpscaleError('superres_tool_invalid',
                           f'{SUPERRES_ENV} 必须包含 {{input}} 和 {{output}} 占位符', 503,
                           blockers=[{'code': 'superres_tool_invalid', 'message': '命令模板缺少占位符'}])
    rendered = template.replace('{input}', str(source)).replace('{output}', str(output))
    argv = shlex.split(rendered, posix=False)
    if not argv:
        raise UpscaleError('superres_tool_invalid', f'{SUPERRES_ENV} 命令为空', 503)
    return argv


def _probe_output(path: Path, source: dict[str, Any], target: dict[str, Any], *,
                  preserve_audio: bool) -> dict[str, Any]:
    backend = _backend()
    facts = backend.authorized_media_facts(str(path))
    if not facts.get('known'):
        raise UpscaleError('output_unreadable', '生成的候选无法读取，未登记', 500)
    if (facts.get('width'), facts.get('height')) != (target['width'], target['height']):
        raise UpscaleError('output_size_mismatch',
                           f'输出尺寸 {facts.get("width")}×{facts.get("height")} 与目标 {target["width"]}×{target["height"]} 不符', 500)
    source_fps = source.get('fps')
    if source_fps and facts.get('fps') and abs(float(facts['fps']) - float(source_fps)) > .1:
        raise UpscaleError('output_fps_mismatch', '输出帧率与源视频不符', 500)
    source_duration = source.get('duration_seconds')
    if source_duration and facts.get('duration_seconds'):
        tolerance = 2 / float(source_fps) if source_fps else .5
        if abs(float(facts['duration_seconds']) - float(source_duration)) > tolerance:
            raise UpscaleError('output_duration_mismatch', '输出时长与源视频不符', 500)
    source_frames = source.get('frames')
    if source_frames and facts.get('frames') and abs(int(facts['frames']) - int(source_frames)) > 1:
        raise UpscaleError('output_frames_mismatch', '输出帧数与源视频不符', 500)
    expected_audio = 1 if preserve_audio and source.get('audio_streams') else 0
    if expected_audio and not facts.get('audio_streams'):
        raise UpscaleError('output_audio_missing', '请求保留音轨，但候选没有音轨', 500)
    if not preserve_audio and facts.get('audio_streams'):
        raise UpscaleError('output_audio_unexpected', '请求生成无音轨候选，但候选包含音轨', 500)
    return facts


def execute(payload: dict[str, Any], *, stopped: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Produce one new candidate file; never writes to the source."""
    backend = _backend()
    source_record = payload['source']
    source = backend.resolve_registered_path(source_record['path'])
    if not source.is_file():
        raise UpscaleError('source_unavailable', '源视频不存在', 409)
    workspace = backend.resolve_registered_path(payload['workspace_root'])
    task_directory = str(payload.get('task_id') or payload['asset_id'])
    output_dir = (workspace / 'delivery-upscale' / task_directory).resolve()
    if workspace.resolve() not in output_dir.parents:
        raise UpscaleError('output_outside_workspace', '候选输出目录越界', 500)
    output_dir.mkdir(parents=True, exist_ok=True)
    method_tag = 'sr2x-lanczos' if payload['method'] == 'superres_2x+lanczos' else 'lanczos'
    stem = ''.join(char if char.isalnum() or char in '-_' else '-' for char in source.stem)[:80] or 'source'
    final = output_dir / f'{stem}-{payload["target"]["resolution"]}-{method_tag}.mp4'
    temporary = output_dir / f'.{uuid.uuid4().hex}.part.mp4'
    intermediate = output_dir / f'.{uuid.uuid4().hex}.2x.mp4'
    log_path = output_dir / f'{uuid.uuid4().hex}.log'
    before = source.stat()
    started = time.monotonic()
    target = payload['target']
    preserve_audio = bool(payload['preserve_audio'])
    audio_streams = int(source_record.get('audio_streams') or 0)
    try:
        if payload['method'] == 'superres_2x+lanczos':
            _run(_superres_argv(source, intermediate), log_path=log_path, stopped=stopped)
            if not intermediate.is_file():
                raise UpscaleError('superres_output_missing', '本地超分运行器没有生成 2x 文件', 500)
            command = _ffmpeg_command(intermediate, temporary, target,
                                      preserve_audio=preserve_audio, audio_streams=audio_streams,
                                      second_input=source)
        else:
            command = _ffmpeg_command(source, temporary, target,
                                      preserve_audio=preserve_audio, audio_streams=audio_streams)
        _run(command, log_path=log_path, stopped=stopped)
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise UpscaleError('source_changed', '源文件在生成期间发生变化；候选未登记', 409)
        facts = _probe_output(temporary, source_record, target, preserve_audio=preserve_audio)
        os.replace(temporary, final)
        elapsed = round(time.monotonic() - started, 3)
        return {
            'status': 'succeeded',
            'asset_id': payload['asset_id'],
            'output': backend.root_relative_path(final),
            'source': payload['source'],
            'target': payload['target'],
            'method': payload['method'],
            'native_detail': bool(payload['native_detail']),
            'preserve_audio': preserve_audio,
            'audio_preserved': bool(preserve_audio and facts.get('audio_streams')),
            'audio_note': ('源视频没有音轨，未保留音频' if preserve_audio and not source_record.get('audio_streams')
                           else '按请求生成无音轨候选' if not preserve_audio
                           else '音轨已随候选保留（重新编码为 AAC）'),
            'output_facts': facts,
            'elapsed_seconds': elapsed,
            'candidate_status': 'pending_review',
            'note': '只生成新候选；未采用、未替换源、未进入装配。',
        }
    except Exception:
        for path in (temporary, intermediate, final):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass


def register_candidate(payload: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Register the new file as a project asset and a project artifact record."""
    backend = _backend()
    project_id = payload['project_id']
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(project_id)
        assets = list(manifest.get('assets') or [])
        asset_id = payload['asset_id']
        source = payload['source']
        existing = next((item for item in assets if isinstance(item, dict) and item.get('id') == asset_id), None)
        if existing is None:
            title = f"{source['asset_id']} · {payload['target']['resolution'].upper()} 交付候选"
            asset = {
                'id': asset_id,
                'kind': 'video',
                'title': title,
                'ready': True,
                'subtitle': f"{payload['method']} · 待人工检查（未采用）",
                'origin': '交付超分候选',
                'sources': {'A': receipt['output']},
                'upscale_task_id': payload.get('task_id'),
                'upscale': {
                    'source': source,
                    'target': payload['target'],
                    'method': payload['method'],
                    'native_detail': payload['native_detail'],
                    'audio_preserved': receipt['audio_preserved'],
                    'elapsed_seconds': receipt['elapsed_seconds'],
                },
            }
            assets.append(asset)
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
    record = backend.append_project_record(
        'artifact:delivery-upscale',
        {'title': f"{source['asset_id']} · {payload['target']['resolution'].upper()} 交付候选",
         'content': {key: receipt[key] for key in ('source', 'target', 'method', 'native_detail',
                                                   'preserve_audio', 'audio_preserved', 'elapsed_seconds',
                                                   'output_facts')},
         'references': [receipt['output'], source['path']],
         'status': 'pending_review'},
        asset_id=asset_id, source='delivery-upscale', project_id=project_id,
    )
    return {'asset_id': asset_id, 'record_revision': record['revision']}


def run_delivery_upscale_task(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one delivery-upscale task, recording a terminal receipt.

    Used by the synchronous HTTP route now and available for a future queued
    dispatch branch. Never adopts, assembles or overwrites the source.
    """
    backend = _backend()
    try:
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        backend.set_task(task_id, status='running')
        receipt = execute({**payload, 'task_id': task_id},
                          stopped=lambda: bool((backend.get_task(task_id) or {}).get('stop_requested')))
        registration = register_candidate({**payload, 'task_id': task_id}, receipt)
        result = {**receipt, **registration, 'task_id': task_id}
        backend.set_task(task_id, status='succeeded', stop_requested=0, error=None,
                         result=json.dumps(result, ensure_ascii=False))
        return {'ok': True, 'task': backend.get_task(task_id) or {}}
    except UpscaleError as exc:
        status = 'stopped' if exc.code == 'upscale_stopped' else 'failed'
        backend.set_task(task_id, status=status, stop_requested=1 if status == 'stopped' else 0,
                         error=str(exc),
                         result=json.dumps({'status': status, **exc.detail}, ensure_ascii=False))
        return {'ok': False, 'error': exc, 'task': backend.get_task(task_id) or {}}
    except Exception as exc:  # noqa: BLE001 - the receipt must record an unknown failure
        backend.set_task(task_id, status='needs_reconcile', stop_requested=0,
                         error=f'交付超分结果待核对: {exc}')
        return {'ok': False,
                'error': UpscaleError('upscale_needs_reconcile', f'交付超分结果待核对: {exc}', 500),
                'task': backend.get_task(task_id) or {}}


def submit_request(project_id: str, request: DeliveryUpscaleRequest) -> dict[str, Any]:
    """Validate one explicit request, record a task and execute it synchronously."""
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    submission = request.model_dump(mode='json')
    if request.source_slot not in SOURCE_SLOTS:
        raise UpscaleError('unknown_source_slot', f'来源阶段必须是 {", ".join(SOURCE_SLOTS)}')
    if request.target_resolution not in TARGET_RESOLUTIONS:
        raise UpscaleError('unknown_target_resolution', f'目标分辨率必须是 {", ".join(TARGET_RESOLUTIONS)}')
    if request.method not in METHODS:
        raise UpscaleError('unknown_method', f'方法必须是 {", ".join(METHODS)}')
    existing = backend.find_submission_task(project_id, request.idempotency_key)
    if existing:
        frozen = (existing.get('payload') or {}).get('submission_receipt', {}).get('request')
        if frozen != submission:
            raise UpscaleError('idempotency_key_conflict',
                               '该请求标识已用于不同的交付超分参数', 409, task_id=existing.get('id'))
        return existing
    facts = capability()
    method = facts['methods'][request.method]
    if not method['available']:
        raise UpscaleError('upscale_tool_unavailable',
                           f'{request.method} 当前不可用', 503,
                           method=request.method, blockers=method['blockers'], methods=facts['methods'])
    _, _, source = _adopted_review(project_id, request.source_asset_id, request.source_slot)
    backend.require_submission_capacity()
    task_id = str(uuid.uuid4())
    payload = _prepare_payload(project_id, manifest, request, source, task_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute(
            'INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (task_id, payload['asset_id'], 'preparing', now, now,
             json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    outcome = run_delivery_upscale_task(task_id, payload)
    if not outcome['ok']:
        error: UpscaleError = outcome['error']
        detail = {**error.detail, 'task_id': task_id}
        raise UpscaleError(error.code, str(error), error.status_code, **{k: v for k, v in detail.items() if k not in {'code', 'message'}})
    return outcome['task']
