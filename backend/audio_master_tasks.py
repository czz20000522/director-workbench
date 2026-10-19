"""Bounded, frozen CPU processing for complete master audio candidates.

The task lifecycle is owned by the existing workbench task table and scheduler.
This module only owns the deterministic PCM processing and receipt validation.
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import re
import shutil
import subprocess
import wave

import numpy as np

from . import audio_edit, creative_inspection, media_operations

MAX_SECONDS = 30 * 60
MAX_BYTES = 2 * 1024 ** 3
CHUNK_FRAMES = 65536

DEFAULTS = {
    'target_lufs': -16.0,
    'true_peak_dbtp': -1.0,
    'fade_in_seconds': 0.0,
    'fade_out_seconds': 0.0,
    'trim_start_seconds': 0.0,
    'trim_duration_seconds': None,
    'background_gain_db': -12.0,
    'ducking': None,
    'pad_silence': False,
}


class AudioMasterStoppedBeforeStart(RuntimeError):
    """No output or processing has started."""


def _db(value: float, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite in [{low}, {high}]')
    return float(value)


def _duration(path: Path) -> tuple[int, int, int, float]:
    with audio_edit.pcm_input(path) as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
    return rate, channels, frames, frames / rate


def _validate_ducking(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'threshold_db', 'ratio', 'attack_seconds', 'release_seconds'}:
        raise ValueError('ducking must contain only threshold_db, ratio, attack_seconds, release_seconds')
    return {
        'threshold_db': _db(value.get('threshold_db', -24), 'ducking.threshold_db', -60, 0),
        'ratio': _db(value.get('ratio', 4), 'ducking.ratio', 1, 20),
        'attack_seconds': _db(value.get('attack_seconds', .01), 'ducking.attack_seconds', .001, 1),
        'release_seconds': _db(value.get('release_seconds', .2), 'ducking.release_seconds', .01, 2),
    }


def validate_parameters(parameters: dict) -> dict:
    if not isinstance(parameters, dict) or set(parameters) - set(DEFAULTS):
        raise ValueError('Unknown audio master parameters')
    result = {**DEFAULTS, **parameters}
    result['target_lufs'] = _db(result['target_lufs'], 'target_lufs', -24, -8)
    result['true_peak_dbtp'] = _db(result['true_peak_dbtp'], 'true_peak_dbtp', -3, 0)
    result['fade_in_seconds'] = _db(result['fade_in_seconds'], 'fade_in_seconds', 0, 10)
    result['fade_out_seconds'] = _db(result['fade_out_seconds'], 'fade_out_seconds', 0, 10)
    result['trim_start_seconds'] = _db(result['trim_start_seconds'], 'trim_start_seconds', 0, MAX_SECONDS * 2)
    if result['trim_duration_seconds'] is not None:
        result['trim_duration_seconds'] = _db(result['trim_duration_seconds'], 'trim_duration_seconds', 0.001, MAX_SECONDS)
    result['background_gain_db'] = _db(result['background_gain_db'], 'background_gain_db', -60, 12)
    if type(result['pad_silence']) is not bool:
        raise ValueError('pad_silence must be boolean')
    result['ducking'] = _validate_ducking(result['ducking'])
    return result


def _check_input(path: Path, *, label: str) -> tuple[int, int, int, float]:
    if not path.is_file():
        raise ValueError(f'{label} must be a file')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f'{label} exceeds the 2 GiB input limit')
    if path.suffix.lower() != '.wav':
        facts = creative_inspection.probe(path)
        duration = facts.get('duration_seconds')
        if not facts.get('audio_streams') or duration is None:
            raise ValueError(f'{label} must be a readable PCM16 WAV or audio file')
        if duration > MAX_SECONDS:
            raise ValueError(f'{label} exceeds the 30 minute limit')
        if not media_operations.FFMPEG.is_file():
            raise ValueError('Local CPU audio decoder is unavailable')
        return 0, 0, 0, float(duration)
    try:
        rate, channels, frames, duration = _duration(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f'{label} must be PCM16 mono/stereo WAV') from exc
    if duration <= 0 or duration > MAX_SECONDS:
        raise ValueError(f'{label} must be longer than zero and at most 30 minutes')
    return rate, channels, frames, duration


def prepare(task_id: str, source: Path, snapshot_root: Path, *, parameters: dict,
            background: Path | None = None) -> dict:
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
        raise ValueError('Invalid task ID')
    if task_id.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('Reserved task ID')
    frozen_parameters = validate_parameters(parameters)
    source = Path(source).resolve(strict=True)
    background = None if background is None else Path(background).resolve(strict=True)
    source_facts = _check_input(source, label='Source master')
    background_facts = _check_input(background, label='Background audio') if background else None
    if source_facts[0] and background_facts and background_facts[0] and source_facts[:2] != background_facts[:2]:
        raise ValueError('Background sample rate and channels must match the source')
    requested = frozen_parameters['trim_duration_seconds']
    if requested is not None and requested > MAX_SECONDS:
        raise ValueError('Selected audio must be at most 30 minutes')
    if frozen_parameters['trim_start_seconds'] >= source_facts[3]:
        raise ValueError('Trim starts beyond the audio')
    if requested is None and source_facts[3] - frozen_parameters['trim_start_seconds'] > MAX_SECONDS:
        raise ValueError('Selected audio must be at most 30 minutes')
    if requested is not None and not frozen_parameters['pad_silence'] and frozen_parameters['trim_start_seconds'] + requested > source_facts[3] + 1e-6:
        raise ValueError('Selected source duration exceeds available audio; set pad_silence explicitly to allow it')
    root = Path(snapshot_root)
    if not root.is_absolute():
        raise ValueError('Trusted snapshot root must be absolute')
    directory = root.resolve() / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen_source = directory / ('source' + source.suffix.lower())
    shutil.copyfile(source, frozen_source)
    frozen_background = None
    if background is not None:
        frozen_background = directory / ('background' + background.suffix.lower())
        shutil.copyfile(background, frozen_background)
    request = {'task_id': task_id, 'kind': 'audio_master', 'source': str(frozen_source),
               'background': None if frozen_background is None else str(frozen_background),
               'parameters': frozen_parameters}
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'audio_master', 'request': request, 'request_path': str(request_path),
            'output_dir': str(directory / 'result')}


def frozen_request(payload: dict) -> tuple[dict, Path]:
    request = payload['request']
    request_path = Path(payload['request_path'])
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('Invalid frozen task payload')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('Request snapshot differs from stored task')
    if set(request) != {'task_id', 'kind', 'source', 'background', 'parameters'} or request['kind'] != 'audio_master':
        raise ValueError('Invalid frozen request schema')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name or Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('Frozen task directory mismatch')
    for key in ('source', 'background'):
        if key == 'background' and request[key] is None:
            continue
        path = Path(request[key])
        if path.suffix.lower() not in {'.wav', '.mp3', '.m4a', '.flac'} or path.resolve() != directory / (key + path.suffix.lower()):
            raise ValueError('Input is outside the frozen task directory')
    validate_parameters(request['parameters'])
    return request, directory / 'result'


def _pcm_source(source: str | Path, *, decode: bool) -> Path:
    path = Path(source)
    if path.suffix.lower() == '.wav':
        return path
    target = path.with_name('decoded-' + path.stem + '.wav')
    if decode:
        subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-n', '-i', str(path),
                        '-vn', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(target)],
                       check=True, timeout=900, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if target.is_symlink() or not target.is_file():
        raise ValueError('Normalized audio input unavailable')
    return target


def _iter_segment(stream, start: int, count: int):
    stream.setpos(start)
    remaining = count
    while remaining:
        frames = min(remaining, CHUNK_FRAMES)
        data = stream.readframes(frames)
        if len(data) != frames * stream.getnchannels() * 2:
            raise ValueError('Truncated PCM WAV')
        yield np.frombuffer(data, dtype='<i2').reshape(-1, stream.getnchannels()).astype(np.float64) / 32768.0
        remaining -= frames


def _lufs(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0
    return -120.0 if rms <= 1e-12 else 20 * math.log10(rms) - 0.691


def _ffmpeg_version() -> str:
    try:
        result = subprocess.run([str(media_operations.FFMPEG), '-version'], capture_output=True, text=True,
                                timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return (result.stdout.splitlines()[0] if result.stdout else '').strip() or 'unavailable'
    except (OSError, AttributeError, subprocess.SubprocessError):
        return 'unavailable (deterministic PCM engine)'


def _read_info(path: Path):
    stream = wave.open(str(path), 'rb')
    rate, channels, total = stream.getframerate(), stream.getnchannels(), stream.getnframes()
    if stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE' or channels not in (1, 2):
        stream.close()
        raise ValueError('Only PCM16 mono/stereo WAV is supported')
    return stream, rate, channels, total


def execute(payload: dict, cancelled=lambda: False) -> dict:
    request, output_dir = frozen_request(payload)
    if cancelled():
        raise AudioMasterStoppedBeforeStart('Audio master processing stopped before execution')
    output_dir.mkdir(exist_ok=False)
    source_path = _pcm_source(request['source'], decode=True)
    background_path = _pcm_source(request['background'], decode=True) if request['background'] else None
    src, rate, channels, total = _read_info(source_path)
    params = validate_parameters(request['parameters'])
    start = round(params['trim_start_seconds'] * rate)
    available = max(0, total - start)
    frames = available if params['trim_duration_seconds'] is None else round(params['trim_duration_seconds'] * rate)
    if frames <= 0 or frames > MAX_SECONDS * rate or start >= total:
        src.close()
        raise ValueError('Selected audio must be nonempty and at most 30 minutes')
    if frames > available and not params['pad_silence']:
        src.close()
        raise ValueError('Selected source duration exceeds available audio; set pad_silence explicitly to allow it')
    bg = None
    if background_path:
        bg, bg_rate, bg_channels, bg_total = _read_info(background_path)
        if (bg_rate, bg_channels) != (rate, channels):
            src.close(); bg.close()
            raise ValueError('Background sample rate and channels must match the source')
        if bg_total < frames and not params['pad_silence']:
            src.close(); bg.close()
            raise ValueError('Background is shorter than the selected master; set pad_silence explicitly to allow it')
    mix_path = output_dir / 'mix.wav'
    with mix_path.open('xb') as handle:
        with wave.open(handle, 'wb') as mix:
            mix.setnchannels(channels); mix.setsampwidth(2); mix.setframerate(rate)
            source_peak = 0.0; clipped = 0
            bg_gain = 10 ** (params['background_gain_db'] / 20)
            duck = params['ducking']
            for index, source_chunk in enumerate(_iter_segment(src, start, min(frames, available))):
                chunk_start = index * CHUNK_FRAMES
                count = len(source_chunk)
                if duck is not None and bg is not None:
                    level = _lufs(source_chunk)
                    over = max(0.0, level - duck['threshold_db'])
                    reduction_db = over * (1 - 1 / duck['ratio'])
                    bg_scale = bg_gain * 10 ** (-reduction_db / 20)
                else:
                    bg_scale = bg_gain
                if bg is not None:
                    if chunk_start < bg_total:
                        take = min(count, bg_total - chunk_start)
                        background_chunk = next(_iter_segment(bg, chunk_start, take))
                        if take < count:
                            background_chunk = np.pad(background_chunk, ((0, count - take), (0, 0)))
                    else:
                        background_chunk = np.zeros_like(source_chunk)
                    source_chunk = source_chunk + background_chunk * bg_scale
                elif count < frames and params['pad_silence']:
                    source_chunk = source_chunk
                source_peak = max(source_peak, float(np.max(np.abs(source_chunk))) if source_chunk.size else 0)
                if chunk_start < round(params['fade_in_seconds'] * rate):
                    fade = min(count, round(params['fade_in_seconds'] * rate) - chunk_start)
                    source_chunk[:fade] *= np.linspace(0, 1, fade, endpoint=True)[:, None]
                fade_start = frames - round(params['fade_out_seconds'] * rate)
                if chunk_start + count > fade_start:
                    begin = max(0, fade_start - chunk_start)
                    source_chunk[begin:] *= np.linspace(1, 0, count - begin, endpoint=True)[:, None]
                pcm = np.rint(np.clip(source_chunk, -1, 1) * 32767).astype('<i2')
                clipped += int(np.count_nonzero((source_chunk < -1) | (source_chunk > 1)))
                mix.writeframes(pcm.tobytes())
            if frames > available:
                zeros = np.zeros((frames - available, channels), dtype='<i2')
                mix.writeframes(zeros.tobytes())
    src.close()
    if bg:
        bg.close()
    # First pass: deterministic integrated loudness and true peak on the mixed file.
    mixed, rate, channels, total = _read_info(mix_path)
    values = []
    peak = 0.0
    blocks = []
    for chunk in _iter_segment(mixed, 0, total):
        peak = max(peak, float(np.max(np.abs(chunk))) if chunk.size else 0)
        values.append(float(np.sum(np.square(chunk))))
        blocks.append(_lufs(chunk))
    rms = math.sqrt(sum(values) / max(1, total * channels))
    input_lufs = -120.0 if rms <= 1e-12 else 20 * math.log10(rms) - 0.691
    lra_values = sorted(value for value in blocks if value > -120)
    lra = (float(np.percentile(lra_values, 95) - np.percentile(lra_values, 10)) if len(lra_values) > 1 else 0.0)
    gain_db = params['target_lufs'] - input_lufs
    limit = 10 ** (params['true_peak_dbtp'] / 20)
    if peak > 1e-12:
        gain_db = min(gain_db, 20 * math.log10(limit / peak))
    gain = 10 ** (gain_db / 20)
    mixed.setpos(0)
    output = output_dir / 'master.wav'
    output_peak = 0.0; output_clipped = clipped
    with output.open('xb') as handle:
        with wave.open(handle, 'wb') as result:
            result.setnchannels(channels); result.setsampwidth(2); result.setframerate(rate)
            for chunk in _iter_segment(mixed, 0, total):
                scaled = chunk * gain
                output_peak = max(output_peak, float(np.max(np.abs(scaled))) if scaled.size else 0)
                output_clipped += int(np.count_nonzero((scaled < -1) | (scaled > 1)))
                result.writeframes(np.rint(np.clip(scaled, -1, 1) * 32767).astype('<i2').tobytes())
    mixed.close()
    report = {
        'output': str(output), 'sample_rate': rate, 'channels': channels, 'frames': total,
        'duration_seconds': total / rate, 'encoding': 'PCM_16', 'input_peak': peak,
        'input_peak_dbtp': -120.0 if peak <= 1e-12 else 20 * math.log10(peak),
        'output_true_peak_dbtp': -120.0 if output_peak <= 1e-12 else 20 * math.log10(output_peak),
        'loudness_lufs_i': input_lufs + gain_db, 'loudness_lra': lra,
        'clipped_samples': output_clipped, 'gain_db_applied': gain_db,
        'parameters': params, 'ffmpeg_version': _ffmpeg_version(),
    }
    receipt = {**request, 'status': 'succeeded', 'output': str(output), 'sample_rate': rate,
               'channels': channels, 'frames': total, 'duration_seconds': total / rate,
               'encoding': 'PCM_16', 'report': report}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return read_receipt(payload)


def read_receipt(payload: dict) -> dict:
    request, output_dir = frozen_request(payload)
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('No successful audio master receipt')
    if any(receipt.get(key) != value for key, value in request.items()):
        raise ValueError('Receipt does not match frozen request')
    output = output_dir / 'master.wav'
    if receipt.get('output') != str(output) or output.is_symlink() or output.resolve() != output:
        raise ValueError('Unexpected output path')
    with audio_edit.pcm_input(output) as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        if frames <= 0 or frames > rate * MAX_SECONDS:
            raise ValueError('Invalid output frame count')
    for key in ('sample_rate', 'channels', 'frames', 'duration_seconds', 'encoding', 'report'):
        if key not in receipt:
            raise ValueError('Audio master receipt metadata is incomplete')
    if (rate, channels, frames) != (receipt['sample_rate'], receipt['channels'], receipt['frames']):
        raise ValueError('Audio master receipt metadata mismatch')
    report = receipt['report']
    required = {'input_peak', 'output_true_peak_dbtp', 'loudness_lufs_i', 'loudness_lra', 'clipped_samples', 'parameters', 'ffmpeg_version'}
    if not isinstance(report, dict) or not required <= set(report):
        raise ValueError('Audio master report is incomplete')
    if report.get('output') != receipt['output'] or report.get('frames') != frames:
        raise ValueError('Audio master execution report mismatch')
    return receipt
