"""Explicit audio loudness analysis and normalization candidates.

Only the caller's explicit target values are used; nothing here chooses a
"recommended" loudness, replaces a segment/master track or touches adoption.
All integrated-LUFS, true-peak and LRA values come from FFmpeg's ``loudnorm``
filter, so the numbers are real measurements rather than sample estimates.

The HTTP router (``backend/routers/audio_delivery.py``) owns project binding,
ownership checks and candidate registration; this module only validates bounded
audio inputs, measures loudness and writes a new PCM16 WAV.
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import re
import subprocess
import tempfile
import time

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from . import audio_edit, creative_inspection, media_operations


METHOD = 'ffmpeg_loudnorm'
SUPPORTED_SUFFIXES = frozenset({'.wav', '.mp3', '.m4a', '.flac'})
MAX_BYTES = 2 * 1024 ** 3
MAX_SECONDS = 30 * 60
MEASURE_TIMEOUT_SECONDS = 300
NORMALIZE_TIMEOUT_SECONDS = 30 * 60
TARGET_LUFS_RANGE = (-24.0, -8.0)
TRUE_PEAK_DB_RANGE = (-3.0, 0.0)
DEFAULT_OUTPUT_SAMPLE_RATE = 48000
DEFAULT_OUTPUT_CHANNELS = 2
ASSEMBLY_BINDING = 'MASTER'


class AudioToolUnavailable(RuntimeError):
    """FFmpeg is missing; callers get a structured blocker instead of a guess."""

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__('本地 FFmpeg 不可用，无法分析或归一音频')


class AudioAnalysisFailed(RuntimeError):
    """FFmpeg was present but loudness measurement did not produce a result."""


class AudioNormalizationFailed(RuntimeError):
    """FFmpeg normalization failed; partial output is not registered."""


class AudioNormalizeRequest(BaseModel):
    """Explicit normalization request; extra fields are rejected."""

    model_config = ConfigDict(extra='forbid')

    asset_id: str = Field(min_length=1, max_length=160)
    source_asset_id: str = Field(min_length=1, max_length=160)
    target_lufs: float
    true_peak_db: float
    idempotency_key: str | None = Field(default=None, max_length=128)


def tool_status() -> dict:
    """Read the existing FFmpeg tool location without importing app state."""
    ffmpeg = Path(media_operations.FFMPEG)
    missing = [] if ffmpeg.is_file() else [str(ffmpeg)]
    return {'available': not missing, 'method': METHOD, 'ffmpeg': str(ffmpeg), 'missing': missing}


def _number(value, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} 必须是 [{low}, {high}] 内的有限数值')
    return float(value)


def validate_target_parameters(target_lufs, true_peak_db) -> dict:
    return {
        'target_lufs': _number(target_lufs, 'target_lufs', *TARGET_LUFS_RANGE),
        'true_peak_db': _number(true_peak_db, 'true_peak_db', *TRUE_PEAK_DB_RANGE),
    }


def validate_binding(asset_id, manifest: dict, plan: dict) -> dict:
    """Return the explicit binding; never infer a different shot or master."""
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError('必须显式指定绑定对象 asset_id')
    binding = asset_id.strip()
    master = str(manifest.get('assembly_asset_id') or ASSEMBLY_BINDING)
    if binding in {ASSEMBLY_BINDING, master}:
        return {'asset_id': binding, 'kind': 'assembly'}
    segments = {str(item.get('id')) for item in plan.get('segments', []) if isinstance(item, dict)}
    if binding in segments:
        return {'asset_id': binding, 'kind': 'segment'}
    raise ValueError('绑定对象必须是现有分镜或 MASTER')


def _source_facts(source: Path) -> dict:
    path = Path(source)
    if not path.is_file():
        raise ValueError('音频源必须是已登记且存在的文件')
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError('音频源超过 2 GiB 上限')
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        allowed = ', '.join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f'音频源格式 {suffix or "(none)"} 不受支持；允许格式：{allowed}')
    facts = creative_inspection.probe(path)
    if not facts.get('audio_streams'):
        raise ValueError('音频源必须包含可读音轨')
    if suffix == '.wav':
        with audio_edit.pcm_input(path) as stream:
            rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        duration = frames / rate
        if not 0 < duration <= MAX_SECONDS:
            raise ValueError('音频源时长必须大于 0 且不超过 30 分钟')
        return {'path': str(path), 'suffix': suffix, 'size_bytes': size, 'duration_seconds': duration,
                'sample_rate': rate, 'channels': channels, 'frames': frames,
                'input_normalization': 'source PCM16 WAV preserved'}
    duration = facts.get('duration_seconds')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('音频源时长必须大于 0 且不超过 30 分钟')
    return {'path': str(path), 'suffix': suffix, 'size_bytes': size, 'duration_seconds': float(duration),
            'sample_rate': DEFAULT_OUTPUT_SAMPLE_RATE, 'channels': DEFAULT_OUTPUT_CHANNELS, 'frames': None,
            'input_normalization': '48kHz stereo PCM16 via FFmpeg'}


def _run_ffmpeg(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run FFmpeg without subprocess.run's reader threads.

    Tests replace the scheduler's ``threading.Thread`` with a no-op stub, so a
    poll loop over temp files keeps this helper usable there as well.
    """
    deadline = time.monotonic() + timeout
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_file,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as exc:
        stderr_file.close()
        raise AudioToolUnavailable([str(media_operations.FFMPEG)]) from exc
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise AudioNormalizationFailed('FFmpeg 执行超过时限，已停止本次音频处理')
            time.sleep(.02)
        process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read().decode('utf-8', errors='replace')
        return subprocess.CompletedProcess(args, process.returncode, '', stderr)
    finally:
        stderr_file.close()


def _parse_loudnorm_json(stderr: str) -> dict:
    matches = list(re.finditer(r'\{[^{}]*\}', stderr or '', flags=re.DOTALL))
    for match in reversed(matches):
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and any(key in value for key in ('input_i', 'input_lra', 'input_tp')):
            return value
    raise AudioAnalysisFailed('FFmpeg loudnorm 未返回可解析的响度测量 JSON')


def _metric(value, name: str) -> float:
    if isinstance(value, str) and value.strip().lower() in {'-inf', '-infinity'}:
        return -120.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AudioAnalysisFailed(f'FFmpeg loudnorm 字段 {name} 缺失或无效')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioAnalysisFailed(f'FFmpeg loudnorm 字段 {name} 缺失或无效') from exc
    if not math.isfinite(result):
        raise AudioAnalysisFailed(f'FFmpeg loudnorm 字段 {name} 不是有限数值')
    return result


def _measure(source: Path) -> dict:
    ffmpeg = Path(media_operations.FFMPEG)
    try:
        result = _run_ffmpeg([
            str(ffmpeg), '-nostdin', '-hide_banner', '-v', 'info', '-i', str(source), '-vn',
            '-af', 'loudnorm=I=-16:TP=-1:LRA=11:print_format=json', '-f', 'null', '-',
        ], timeout=MEASURE_TIMEOUT_SECONDS)
    except AudioNormalizationFailed as exc:
        raise AudioAnalysisFailed(str(exc)) from exc
    if result.returncode:
        detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
        raise AudioAnalysisFailed(f'FFmpeg 响度分析失败: {detail[0][-500:]}')
    measured = _parse_loudnorm_json(result.stderr)
    fields = {'input_i': 'integrated_lufs', 'input_lra': 'lra', 'input_tp': 'true_peak_db'}
    values = {name: _metric(measured.get(name), name) for name in fields}
    return {
        'integrated_lufs': values['input_i'],
        'true_peak_db': values['input_tp'],
        'lra': max(0.0, values['input_lra']),
        'silence_metrics': [name for name in fields
                            if isinstance(measured.get(name), str)
                            and measured[name].strip().lower() in {'-inf', '-infinity'}],
        'method': METHOD,
    }


def analyze(source: Path) -> dict:
    status = tool_status()
    if not status['available']:
        raise AudioToolUnavailable(status['missing'])
    path = Path(source)
    facts = _source_facts(path)
    measurement = _measure(path)
    return {'source': facts, 'measurement': measurement, 'method': METHOD, 'measured_at': time.time()}


def _count_clipped(path: Path) -> int:
    clipped = 0
    with audio_edit.pcm_input(path) as stream:
        while True:
            data = stream.readframes(65536)
            if not data:
                break
            samples = np.frombuffer(data, dtype='<i2')
            clipped += int(np.count_nonzero((samples <= -32767) | (samples >= 32767)))
    return clipped


def normalize(source: Path, output: Path, *, target_lufs, true_peak_db,
              output_root: Path | None = None) -> dict:
    """Write a new PCM16 WAV candidate; the caller's source file is never touched.

    ``output_root`` is a trusted server path supplied by the router; request
    parameters never choose it.
    """
    status = tool_status()
    if not status['available']:
        raise AudioToolUnavailable(status['missing'])
    target = validate_target_parameters(target_lufs, true_peak_db)
    source = Path(source)
    output = Path(output)
    if output.suffix.lower() != '.wav':
        raise ValueError('候选输出必须是 PCM16 WAV')
    if output_root is not None and not output.resolve().is_relative_to(Path(output_root).resolve()):
        raise ValueError('候选输出必须位于作品目录内')
    if output.exists():
        raise FileExistsError(f'候选输出已存在: {output}')
    source_facts = _source_facts(source)
    before = _measure(source)
    rate, channels = source_facts['sample_rate'], source_facts['channels']
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = Path(media_operations.FFMPEG)
    result = _run_ffmpeg([
        str(ffmpeg), '-nostdin', '-hide_banner', '-v', 'error', '-n', '-i', str(source), '-vn',
        '-af', f"loudnorm=I={target['target_lufs']}:TP={target['true_peak_db']}:LRA=11",
        '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', str(output),
    ], timeout=NORMALIZE_TIMEOUT_SECONDS)
    if result.returncode:
        output.unlink(missing_ok=True)
        detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
        raise AudioNormalizationFailed(f'FFmpeg 响度归一失败: {detail[0][-500:]}')
    after = _measure(output)
    with audio_edit.pcm_input(output) as stream:
        frames, sample_rate, output_channels = stream.getnframes(), stream.getframerate(), stream.getnchannels()
    if frames <= 0 or sample_rate != rate or output_channels != channels:
        output.unlink(missing_ok=True)
        raise AudioNormalizationFailed('FFmpeg 输出与请求的采样率/声道/帧数不一致')
    output_peak = after['true_peak_db']
    limit_met = output_peak <= target['true_peak_db']
    return {
        'method': METHOD,
        'source': source_facts,
        'output': str(output),
        'parameters': dict(target),
        'input_measurement': before,
        'output_measurement': after,
        'sample_rate': sample_rate,
        'channels': output_channels,
        'frames': frames,
        'duration_seconds': frames / sample_rate,
        'encoding': 'PCM_16',
        'clipped_samples': _count_clipped(output),
        'gain_db_applied': round(after['integrated_lufs'] - before['integrated_lufs'], 3),
        'true_peak_limit_met': limit_met,
        'normalization_warning': None if limit_met else '测得的输出 true peak 超过显式目标；采用前请试听核对',
        'created_at': time.time(),
    }


def find_existing_candidate(manifest: dict, *, idempotency_key: str, binding: dict,
                            source_asset_id: str, target: dict) -> dict | None:
    """Return a previous candidate for the same explicit request, if recorded."""
    for asset in manifest.get('assets') or []:
        if not isinstance(asset, dict):
            continue
        record = asset.get('audio_normalization')
        if not isinstance(record, dict):
            continue
        if (record.get('idempotency_key') == idempotency_key
                and record.get('bound_asset_id') == binding['asset_id']
                and record.get('source_asset_id') == source_asset_id
                and record.get('target_lufs') == target['target_lufs']
                and record.get('true_peak_db') == target['true_peak_db']
                and isinstance(record.get('method'), str)):
            return asset
    return None


def candidate_response(candidate: dict, *, reused: bool) -> dict:
    """Public receipt for a registered candidate; adoption remains explicit."""
    record = candidate.get('audio_normalization') if isinstance(candidate.get('audio_normalization'), dict) else {}
    return {
        'candidate_asset_id': candidate.get('id'),
        'asset_id': record.get('bound_asset_id'),
        'binding': {'asset_id': record.get('bound_asset_id'), 'kind': record.get('binding_kind')},
        'source_asset_id': record.get('source_asset_id'),
        'source_asset': record.get('source_asset'),
        'target': {'target_lufs': record.get('target_lufs'), 'true_peak_db': record.get('true_peak_db')},
        'method': record.get('method'),
        'report': record.get('report'),
        'candidate': candidate,
        'adopted': False,
        'reused': reused,
        'note': '新音频候选已登记为作品素材；未修改分镜、母版或装配，采用仍需显式操作。',
    }
