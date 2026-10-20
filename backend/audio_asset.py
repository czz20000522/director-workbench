"""Extract an adopted video's audio track into a new project audio asset.

The workbench only reads one explicitly adopted video candidate/slot and writes
a *new* PCM16 WAV asset into the project workspace:

* the source is never modified or used as the output path;
* loudness is never normalized, the assembly track is never replaced, and no
  model/plugin is downloaded;
* adoption and binding stay untouched; the new asset is simply available for the
  existing audio/analysis and audio/normalize entries;
* a missing local tool is a structured blocker instead of a guessed fallback.

``backend/routers/audio_asset.py`` owns the HTTP contract (project ownership is
still enforced by the existing private-mode middleware); this module owns
adopted-source validation, bounded FFmpeg extraction, WAV facts, idempotency and
manifest registration.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .media_operations import FFMPEG

METHOD = 'ffmpeg_pcm_s16le'
SOURCE_SLOTS = ('sample', 'finish', 'final')
SLOT_LABELS = {'sample': '样片', 'finish': '精细候选', 'final': '成片'}
ASSEMBLY_BINDING = 'MASTER'
OUTPUT_RATE_RANGE = (8000, 96000)
OUTPUT_CHANNELS = (1, 2)
FALLBACK_SAMPLE_RATE = 48000
FALLBACK_CHANNELS = 2
EXTRACTION_TIMEOUT_SECONDS = 30 * 60
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,160}')
ASSET_DIR = 'audio-extract'


class AudioAssetError(RuntimeError):
    """A structured validation/tool blocker the HTTP layer can expose."""

    def __init__(self, code: str, message: str, status_code: int = 422, **details: Any):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details

    @property
    def detail(self) -> dict[str, Any]:
        return {'code': self.code, 'message': str(self), **self.details}


def http_exception(error: AudioAssetError) -> HTTPException:
    """Translate one structured blocker into the shared FastAPI error shape."""
    return HTTPException(error.status_code, error.detail)


class AudioExtractRequest(BaseModel):
    """One explicit extraction request; nothing here is inferred."""

    model_config = ConfigDict(extra='forbid')

    source_asset_id: str = Field(min_length=1, max_length=160)
    source_slot: Literal['sample', 'finish', 'final']
    audio_asset_id: str | None = Field(default=None, max_length=160)

    @field_validator('source_asset_id')
    @classmethod
    def _source_identifier(cls, value: str) -> str:
        if not value.strip() or '/' in value or '\\' in value or value in {'.', '..'}:
            raise ValueError('source_asset_id 必须是非空的分镜/MASTER 标识，不能包含路径')
        return value

    @field_validator('audio_asset_id')
    @classmethod
    def _audio_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not IDENTIFIER.fullmatch(value):
            raise ValueError('audio_asset_id 只能包含字母、数字、下划线和连字符，且不能是路径')
        return value


def _backend():
    # Imported lazily so this module never participates in app.py's import cycle.
    from . import app as backend
    return backend


def tool_status() -> dict[str, Any]:
    """Report the existing local FFmpeg location without importing app state."""
    ffmpeg = Path(FFMPEG)
    missing = [] if ffmpeg.is_file() else [str(ffmpeg)]
    return {'available': not missing, 'method': METHOD, 'ffmpeg': str(ffmpeg), 'missing': missing}


# --------------------------------------------------------------- media facts

def _duration_seconds(stream, container) -> float | None:
    try:
        if stream is not None and stream.duration is not None and stream.time_base is not None:
            value = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            value = float(container.duration / 1_000_000)
        else:
            return None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(value, 6) if math.isfinite(value) and value > 0 else None


def probe_audio(path: Path) -> dict[str, Any]:
    """Read audio-stream facts with the project's existing PyAV probe stack.

    Only facts are returned; the caller decides whether extraction is allowed.
    """
    try:
        import av
    except ImportError as exc:
        raise AudioAssetError('audio_probe_unavailable', '本地媒体探测组件不可用，无法确认音轨',
                              503, missing=['av']) from exc
    path = Path(path)
    if not path.is_file():
        raise AudioAssetError('source_unavailable', '来源视频文件不存在', 409)
    try:
        with av.open(str(path)) as container:
            audio = next(iter(container.streams.audio), None)
            video = next(iter(container.streams.video), None)
            duration = _duration_seconds(audio or video, container)
            if audio is None:
                return {'known': True, 'audio_streams': 0, 'duration_seconds': duration,
                        'sample_rate': None, 'channels': None,
                        'width': video.width if video else None,
                        'height': video.height if video else None}
            sample_rate = audio.rate
            try:
                sample_rate = int(sample_rate) if sample_rate is not None else None
            except (TypeError, ValueError):
                sample_rate = None
            channels = audio.channels
            return {'known': True, 'audio_streams': 1, 'duration_seconds': duration,
                    'sample_rate': sample_rate, 'channels': channels,
                    'width': video.width if video else None,
                    'height': video.height if video else None}
    except AudioAssetError:
        raise
    except Exception as exc:  # PyAV wraps codec/container errors in several types.
        raise AudioAssetError('source_unreadable', f'无法读取来源视频媒体信息: {exc}', 409) from exc


def _limit_facts() -> dict[str, Any]:
    return {'sample_rate_min': OUTPUT_RATE_RANGE[0], 'sample_rate_max': OUTPUT_RATE_RANGE[1],
            'channels': list(OUTPUT_CHANNELS)}


def _format_conversion(sample_rate: int | None, channels: int | None) -> dict[str, Any]:
    converted_rate = not (isinstance(sample_rate, int) and OUTPUT_RATE_RANGE[0] <= sample_rate <= OUTPUT_RATE_RANGE[1])
    converted_channels = channels not in OUTPUT_CHANNELS
    return {
        'sample_rate': f'resampled_to_{FALLBACK_SAMPLE_RATE}' if converted_rate else 'preserved',
        'channels': f'downmixed_to_{FALLBACK_CHANNELS}' if converted_channels else 'preserved',
        'source_sample_rate': sample_rate,
        'source_channels': channels,
        'loudness_normalized': False,
    }


def extraction_arguments(source: Path, output: Path, *, sample_rate: int | None,
                         channels: int | None) -> list[str]:
    """Build the bounded FFmpeg command; no loudness/assembly step is added."""
    args = [str(FFMPEG), '-nostdin', '-hide_banner', '-v', 'error', '-n',
            '-i', str(source), '-vn', '-map', '0:a:0', '-c:a', 'pcm_s16le']
    if not (isinstance(sample_rate, int) and OUTPUT_RATE_RANGE[0] <= sample_rate <= OUTPUT_RATE_RANGE[1]):
        args += ['-ar', str(FALLBACK_SAMPLE_RATE)]
    if channels not in OUTPUT_CHANNELS:
        args += ['-ac', str(FALLBACK_CHANNELS)]
    args.append(str(output))
    return args


def _run_ffmpeg(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run FFmpeg without reader threads (the fixtures stub ``threading.Thread``)."""
    deadline = time.monotonic() + timeout
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_file,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as exc:
        stderr_file.close()
        raise AudioAssetError('audio_tool_unavailable', '本地 FFmpeg 无法启动',
                              503, missing=[str(FFMPEG)], method=METHOD) from exc
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise AudioAssetError('audio_extract_failed', 'FFmpeg 抽取音轨超过时限，已停止本次处理', 502)
            time.sleep(.02)
        process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read().decode('utf-8', errors='replace')
        return subprocess.CompletedProcess(args, process.returncode, '', stderr)
    finally:
        stderr_file.close()


def extract_track(source: Path, output: Path, *, sample_rate: int | None,
                  channels: int | None) -> None:
    """Write one new PCM16 WAV; the source file is only read."""
    status = tool_status()
    if not status['available']:
        raise AudioAssetError('audio_tool_unavailable', '本地 FFmpeg 不可用，无法抽取视频音轨',
                              503, missing=status['missing'], method=METHOD)
    output = Path(output)
    if output.exists():
        raise AudioAssetError('audio_output_exists', f'音频输出已存在: {output.name}', 409)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = _run_ffmpeg(extraction_arguments(source, output, sample_rate=sample_rate, channels=channels),
                         timeout=EXTRACTION_TIMEOUT_SECONDS)
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        tail = (result.stderr or '').strip().splitlines()[-1:] or ['']
        raise AudioAssetError('audio_extract_failed', f'FFmpeg 抽取音轨失败: {tail[0][-500:]}', 502)


def _wav_peak(stream: wave.Wave_read) -> int:
    peak = 0
    while True:
        data = stream.readframes(65536)
        if not data:
            return peak
        samples = np.frombuffer(data, dtype='<i2')
        if samples.size:
            peak = max(peak, int(np.max(np.abs(samples.astype(np.int32)))))


def wav_facts(path: Path) -> dict[str, Any]:
    """Measure one extracted PCM16 WAV without trusting FFmpeg's log."""
    try:
        with wave.open(str(path), 'rb') as stream:
            if stream.getcomptype() != 'NONE' or stream.getsampwidth() != 2:
                raise AudioAssetError('audio_output_invalid', '抽取结果不是 PCM16 WAV', 502)
            channels, sample_rate, frames = stream.getnchannels(), stream.getframerate(), stream.getnframes()
            peak_samples = _wav_peak(stream)
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioAssetError('audio_output_invalid', f'无法读取抽取结果 WAV: {exc}', 502) from exc
    if frames <= 0 or sample_rate <= 0:
        raise AudioAssetError('audio_output_invalid', '抽取结果 WAV 没有有效帧', 502)
    peak = peak_samples / 32768.0
    return {
        'duration_seconds': round(frames / sample_rate, 6),
        'sample_rate': sample_rate,
        'channels': channels,
        'frames': frames,
        'peak': round(peak, 6),
        'peak_dbfs': round(20 * math.log10(peak), 3) if peak > 0 else -120.0,
        'encoding': 'PCM_16',
    }


# ------------------------------------------------------- adopted source check

def _slot_path(document: dict[str, Any], manifest: dict[str, Any], source_asset_id: str,
               source_slot: str) -> tuple[Path | None, str]:
    assembly_id = str(manifest.get('assembly_asset_id') or ASSEMBLY_BINDING)
    if source_slot == 'final':
        if source_asset_id != assembly_id:
            raise AudioAssetError('assembly_source_mismatch', f'成片来源必须是 {assembly_id}', 422)
        return (document.get('assembly') or {}).get('output'), assembly_id
    segment = next((item for item in document.get('segments', [])
                    if isinstance(item, dict) and str(item.get('id')) == source_asset_id), None)
    if segment is None:
        raise AudioAssetError('segment_not_found', f'分镜不存在: {source_asset_id}', 404)
    video = segment.get('video') or {}
    path = video.get('path') if source_slot == 'sample' else (video.get('finish_review') or {}).get('path')
    return path, assembly_id


def resolve_adopted_source(project_id: str, source_asset_id: str, source_slot: str) -> dict[str, Any]:
    """Accept only the visible, explicitly adopted candidate of that exact slot."""
    backend = _backend()
    if source_slot not in SOURCE_SLOTS:
        raise AudioAssetError('unknown_source_slot', f'来源阶段必须是 {", ".join(SOURCE_SLOTS)}')
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    state = backend.build_project_state(project_id)
    label = SLOT_LABELS[source_slot]
    review = (state.get('reviews') or {}).get(source_asset_id, {}).get(source_slot)
    if not isinstance(review, dict) or review.get('status') != 'approved':
        raise AudioAssetError('source_not_adopted',
                              f'{source_asset_id} 的{label}尚未明确采用；只能抽取已采用/成片的音轨', 409)
    if review.get('source') != 'explicit-adoption' or not isinstance(review.get('adoption_binding'), dict):
        raise AudioAssetError('explicit_adoption_required',
                              f'{source_asset_id} 的{label}不是带候选身份的明确采用记录', 409)
    binding = review['adoption_binding']
    slot_path, assembly_id = _slot_path(document, manifest, source_asset_id, source_slot)
    candidate_ref = review.get('candidate_ref')
    if not isinstance(candidate_ref, str) or not candidate_ref:
        raise AudioAssetError('source_candidate_missing', f'{source_asset_id} 的{label}缺少明确候选引用', 409)
    try:
        path = backend.adoption_candidate_path(candidate_ref, project_id)
    except (HTTPException, ValueError, OSError, RuntimeError) as exc:
        raise AudioAssetError('source_unavailable', f'采用候选不可访问: {exc}', 409) from exc
    if not path.is_file():
        raise AudioAssetError('source_unavailable', '采用候选文件不存在', 409)
    if not backend.same_registered_media(str(slot_path or ''), str(path)):
        raise AudioAssetError('source_changed',
                              '当前采用候选与分镜/成片记录不一致；请重新核对并明确采用当前候选', 409)
    try:
        stat = path.stat()
    except OSError as exc:
        raise AudioAssetError('source_unavailable', f'采用候选不可读: {exc}', 409) from exc
    file_identity = binding.get('file_identity') or {}
    if (file_identity.get('size_bytes') != stat.st_size
            or file_identity.get('modified_ns') != stat.st_mtime_ns):
        raise AudioAssetError('adoption_candidate_changed',
                              '采用候选文件已变化；请重新采用当前候选后再抽取音轨', 409)
    if source_slot != 'final' and binding.get('segment_signature') != backend.adoption_segment_signature(document, source_asset_id):
        raise AudioAssetError('adoption_stale',
                              '该分镜内容或当前候选在采用后已变化；请重新明确采用后再抽取音轨', 409)
    return {
        'path': path,
        'slot': source_slot,
        'asset_id': source_asset_id,
        'candidate_ref': candidate_ref,
        'review': review,
        'assembly_id': assembly_id,
        'file_identity': {'size_bytes': stat.st_size, 'modified_ns': stat.st_mtime_ns},
    }


# ------------------------------------------------------------ idempotency

def request_fingerprint(project_id: str, source: dict[str, Any]) -> str:
    """Stable identity of one extraction request, including the adopted file."""
    payload = {
        'project_id': project_id,
        'source_asset_id': source['asset_id'],
        'source_slot': source['slot'],
        'candidate_ref': source['candidate_ref'],
        'file_identity': source['file_identity'],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def default_asset_id(fingerprint: str) -> str:
    return f'audio-extract-{fingerprint[:16]}'


def find_extraction(manifest: dict[str, Any], fingerprint: str) -> dict[str, Any] | None:
    for asset in manifest.get('assets') or []:
        if not isinstance(asset, dict):
            continue
        record = asset.get('audio_extract')
        if isinstance(record, dict) and record.get('idempotency') == fingerprint:
            return asset
    return None


def _asset_by_id(manifest: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    return next(((asset for asset in manifest.get('assets') or []
                  if isinstance(asset, dict) and asset.get('id') == asset_id)), None)


# ------------------------------------------------------------- registration

def _asset_record(project_id: str, asset_id: str, source: dict[str, Any], facts: dict[str, Any],
                  output_facts: dict[str, Any], output_rel: str, fingerprint: str) -> dict[str, Any]:
    conversion = _format_conversion(facts.get('sample_rate'), facts.get('channels'))
    return {
        'id': asset_id,
        'kind': 'audio',
        'title': f"{SLOT_LABELS[source['slot']]}音轨 · {source['asset_id']}",
        'subtitle': '视频音轨抽取 · PCM16 WAV · 待试听',
        'ready': True,
        'origin': 'CPU音轨抽取',
        'sources': {'A': output_rel},
        'start': 0,
        'end': output_facts['duration_seconds'],
        'audio_extract': {
            'method': METHOD,
            'project_id': project_id,
            'source_asset_id': source['asset_id'],
            'source_slot': source['slot'],
            'source_candidate_ref': source['candidate_ref'],
            'source_file_identity': source['file_identity'],
            'source_facts': {
                'duration_seconds': facts.get('duration_seconds'),
                'sample_rate': facts.get('sample_rate'),
                'channels': facts.get('channels'),
                'audio_streams': facts.get('audio_streams'),
                'width': facts.get('width'),
                'height': facts.get('height'),
            },
            'idempotency': fingerprint,
            'requested_audio_asset_id': asset_id,
            'created_at': time.time(),
            'output': output_rel,
            'duration_seconds': output_facts['duration_seconds'],
            'sample_rate': output_facts['sample_rate'],
            'channels': output_facts['channels'],
            'frames': output_facts['frames'],
            'peak': output_facts['peak'],
            'peak_dbfs': output_facts['peak_dbfs'],
            'peak_metric': 'sample_peak_dbfs',
            'format_conversion': conversion,
            'limits': _limit_facts(),
            'loudness_normalized': False,
            'assembly_modified': False,
            'model_downloaded': False,
        },
    }


def extraction_response(asset: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    """Public receipt for one registered extraction; adoption is still explicit."""
    record = asset.get('audio_extract') if isinstance(asset.get('audio_extract'), dict) else {}
    return {
        'project_id': record.get('project_id'),
        'asset_id': asset.get('id'),
        'media_ref': f"asset:{asset.get('id')}:A",
        'audio_asset': asset,
        'source': {
            'asset_id': record.get('source_asset_id'),
            'slot': record.get('source_slot'),
            'candidate_ref': record.get('source_candidate_ref'),
            'file_identity': record.get('source_file_identity'),
        },
        'extraction': {
            'method': record.get('method'),
            'output': record.get('output'),
            'duration_seconds': record.get('duration_seconds'),
            'sample_rate': record.get('sample_rate'),
            'channels': record.get('channels'),
            'frames': record.get('frames'),
            'peak': record.get('peak'),
            'peak_dbfs': record.get('peak_dbfs'),
            'peak_metric': record.get('peak_metric'),
            'format_conversion': record.get('format_conversion'),
            'created_at': record.get('created_at'),
        },
        'reused': reused,
        'note': '只新增音频素材；未做响度归一、未替换装配音轨、未下载模型。采用/绑定仍须显式操作。',
    }


def _register(project_id: str, asset_id: str, fingerprint: str, output: Path,
              source: dict[str, Any], facts: dict[str, Any], output_facts: dict[str, Any]) -> dict[str, Any]:
    backend = _backend()
    output_rel = backend.root_relative_path(output)
    candidate = _asset_record(project_id, asset_id, source, facts, output_facts, output_rel, fingerprint)
    with backend.MATERIAL_REUSE_LOCK:
        current = backend.require_project_manifest(project_id)
        existing = find_extraction(current, fingerprint)
        if existing is not None:
            output.unlink(missing_ok=True)
            return existing
        if _asset_by_id(current, asset_id) is not None:
            output.unlink(missing_ok=True)
            raise AudioAssetError('audio_asset_id_in_use', f'音频素材 ID 已被占用: {asset_id}', 409)
        assets = list(current.get('assets') or [])
        assets.append(candidate)
        current['assets'] = assets
        backend.save_project_manifest(current)
        if project_id == backend.CURRENT_PROJECT_ID:
            backend.apply_project_manifest(current)
    return candidate


def extract_audio_asset(project_id: str, request: AudioExtractRequest) -> dict[str, Any]:
    """Validate, extract and register one new audio asset; replay safely."""
    backend = _backend()
    backend.require_project_manifest(project_id)
    source = resolve_adopted_source(project_id, request.source_asset_id, request.source_slot)
    fingerprint = request_fingerprint(project_id, source)
    manifest = backend.require_project_manifest(project_id)
    existing = find_extraction(manifest, fingerprint)
    if existing is not None:
        if request.audio_asset_id and request.audio_asset_id != existing.get('id'):
            raise AudioAssetError('audio_asset_id_conflict',
                                  '该来源已抽取为另一个音频素材；幂等重放不能改名', 409)
        record = existing.get('audio_extract') or {}
        try:
            registered = backend.resolve_registered_path(str((existing.get('sources') or {}).get('A')))
        except (OSError, RuntimeError, ValueError) as exc:
            raise AudioAssetError('audio_output_missing', f'既有抽取文件不可读: {exc}', 409) from exc
        if not registered.is_file():
            raise AudioAssetError('audio_output_missing', '既有抽取文件不存在，请重新抽取', 409)
        return extraction_response(existing, reused=True)

    status = tool_status()
    if not status['available']:
        raise AudioAssetError('audio_tool_unavailable', '本地 FFmpeg 不可用，无法抽取视频音轨',
                              503, missing=status['missing'], method=METHOD)
    facts = probe_audio(source['path'])
    if not facts.get('known'):
        raise AudioAssetError('source_unreadable', '无法读取来源视频媒体信息', 409)
    if not facts.get('audio_streams'):
        raise AudioAssetError('source_has_no_audio', '来源视频没有可提取的音轨', 422,
                              source_asset_id=source['asset_id'], source_slot=source['slot'])
    if facts.get('sample_rate') is None or facts.get('channels') is None:
        raise AudioAssetError('source_audio_unreadable', '无法确认来源音轨的采样率或声道，不能安全抽取', 409,
                              source_facts={'sample_rate': facts.get('sample_rate'), 'channels': facts.get('channels')})

    asset_id = request.audio_asset_id or default_asset_id(fingerprint)
    if _asset_by_id(manifest, asset_id) is not None:
        raise AudioAssetError('audio_asset_id_in_use', f'音频素材 ID 已被占用: {asset_id}', 409)

    workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
    output = workspace / ASSET_DIR / f'{asset_id}.wav'
    extract_track(source['path'], output, sample_rate=facts.get('sample_rate'), channels=facts.get('channels'))
    try:
        output_facts = wav_facts(output)
    except AudioAssetError:
        output.unlink(missing_ok=True)
        raise
    if not (OUTPUT_RATE_RANGE[0] <= output_facts['sample_rate'] <= OUTPUT_RATE_RANGE[1]
            and output_facts['channels'] in OUTPUT_CHANNELS):
        output.unlink(missing_ok=True)
        raise AudioAssetError('audio_extract_failed',
                              '抽取结果不是下游可用的单/双声道 8–96 kHz PCM16 WAV', 502,
                              output_facts=output_facts)
    try:
        asset = _register(project_id, asset_id, fingerprint, output, source, facts, output_facts)
    except AudioAssetError:
        output.unlink(missing_ok=True)
        raise
    if asset.get('id') != asset_id:
        # Another request registered the same extraction first; ours is redundant.
        output.unlink(missing_ok=True)
        return extraction_response(asset, reused=True)
    return extraction_response(asset, reused=False)


def list_audio_assets(project_id: str) -> dict[str, Any]:
    """List this project's registered audio assets as facts; choose nothing."""
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    rows = []
    for asset in manifest.get('assets') or []:
        if not isinstance(asset, dict) or asset.get('kind') != 'audio':
            continue
        record = asset.get('audio_extract') if isinstance(asset.get('audio_extract'), dict) else {}
        rows.append({
            'asset_id': asset.get('id'),
            'title': asset.get('title'),
            'ready': bool(asset.get('ready')),
            'origin': asset.get('origin'),
            'media_ref': f"asset:{asset.get('id')}:A",
            'source': (asset.get('sources') or {}).get('A'),
            'created_at': record.get('created_at'),
            'extraction': ({
                'source_asset_id': record.get('source_asset_id'),
                'source_slot': record.get('source_slot'),
                'method': record.get('method'),
                'duration_seconds': record.get('duration_seconds'),
                'sample_rate': record.get('sample_rate'),
                'channels': record.get('channels'),
                'peak': record.get('peak'),
                'peak_dbfs': record.get('peak_dbfs'),
                'peak_metric': record.get('peak_metric'),
            } if record else None),
        })
    return {'project_id': project_id, 'audio_assets': rows, 'total': len(rows)}
