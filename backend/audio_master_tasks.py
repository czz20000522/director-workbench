"""Bounded, frozen CPU processing for complete master audio candidates.

The task lifecycle is owned by the existing workbench task table and scheduler.
This module owns deterministic PCM mixing, FFmpeg loudness measurement and
receipt validation. All measurements reported as LUFS-I, LRA or dBTP come from
FFmpeg's ``loudnorm`` filter rather than sample-level estimates.
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import re
import subprocess
import tempfile
import time
import wave

import numpy as np

from . import audio_edit, creative_inspection, media_operations, secure_files

MAX_SECONDS = 30 * 60
MAX_BYTES = 2 * 1024 ** 3
CHUNK_FRAMES = 65536
EXECUTION_TIMEOUT_SECONDS = MAX_SECONDS
SUPPORTED_INPUT_SUFFIXES = frozenset({'.wav', '.mp3', '.m4a', '.flac'})

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


class AudioMasterExecutionStopped(RuntimeError):
    """Processing was cancelled after output work had started."""


class AudioMasterTimedOut(RuntimeError):
    """The bounded execution deadline elapsed; partial output is retained."""


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
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        allowed = ', '.join(sorted(SUPPORTED_INPUT_SUFFIXES))
        raise ValueError(f'{label} format {suffix or "(none)"} is unsupported; allowed formats: {allowed}')
    if suffix != '.wav':
        facts = creative_inspection.probe(path)
        duration = facts.get('duration_seconds')
        if not facts.get('audio_streams') or duration is None:
            raise ValueError(f'{label} must be a readable audio file')
        if duration <= 0 or duration > MAX_SECONDS:
            raise ValueError(f'{label} must be longer than zero and at most 30 minutes')
        if not media_operations.FFMPEG.is_file():
            raise ValueError('Local CPU audio decoder is unavailable')
        # Non-WAV input is explicitly normalized to 48 kHz stereo PCM16.
        return 0, 0, 0, float(duration)
    try:
        rate, channels, frames, duration = _duration(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f'{label} must be PCM16 mono/stereo WAV') from exc
    if duration <= 0 or duration > MAX_SECONDS:
        raise ValueError(f'{label} must be longer than zero and at most 30 minutes')
    return rate, channels, frames, duration


def _identity_dict(identity: secure_files.FileIdentity) -> dict[str, int]:
    return {'device': identity.device, 'inode': identity.inode, 'size': identity.size,
            'modified_ns': identity.modified_ns, 'mode': identity.mode}


def _copy_frozen(source: Path, destination: Path) -> tuple[secure_files.FileIdentity, secure_files.FileIdentity]:
    checked, identity = secure_files.checked_file(source)
    secure_files.copy_checked(checked, destination, identity)
    _, frozen_identity = secure_files.checked_file(destination)
    return identity, frozen_identity


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
    source_identity, frozen_source_identity = _copy_frozen(source, frozen_source)
    frozen_background = None
    background_identity = frozen_background_identity = None
    if background is not None:
        frozen_background = directory / ('background' + background.suffix.lower())
        background_identity, frozen_background_identity = _copy_frozen(background, frozen_background)
    request = {
        'task_id': task_id, 'kind': 'audio_master', 'source': str(frozen_source),
        'background': None if frozen_background is None else str(frozen_background),
        'source_identity': _identity_dict(source_identity),
        'frozen_source_identity': _identity_dict(frozen_source_identity),
        'background_identity': None if background_identity is None else _identity_dict(background_identity),
        'frozen_background_identity': None if frozen_background_identity is None else _identity_dict(frozen_background_identity),
        'parameters': frozen_parameters,
    }
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'audio_master', 'request': request, 'request_path': str(request_path),
            'output_dir': str(directory / 'result')}


def _validate_identity(value, *, allow_none=False):
    if value is None and allow_none:
        return
    if not isinstance(value, dict) or set(value) != {'device', 'inode', 'size', 'modified_ns', 'mode'}:
        raise ValueError('Invalid frozen input identity')
    if any(type(value[key]) is not int or value[key] < 0 for key in value):
        raise ValueError('Invalid frozen input identity')


def frozen_request(payload: dict) -> tuple[dict, Path]:
    request = payload['request']
    request_path = Path(payload['request_path'])
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('Invalid frozen task payload')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('Request snapshot differs from stored task')
    expected = {'task_id', 'kind', 'source', 'background', 'source_identity', 'frozen_source_identity',
                'background_identity', 'frozen_background_identity', 'parameters'}
    if set(request) != expected or request['kind'] != 'audio_master':
        raise ValueError('Invalid frozen request schema')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name or Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('Frozen task directory mismatch')
    _validate_identity(request['source_identity'])
    _validate_identity(request['frozen_source_identity'])
    _validate_identity(request['background_identity'], allow_none=True)
    _validate_identity(request['frozen_background_identity'], allow_none=True)
    for key in ('source', 'background'):
        if key == 'background' and request[key] is None:
            continue
        path = Path(request[key])
        if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES or path.resolve() != directory / (key + path.suffix.lower()):
            raise ValueError('Input is outside the frozen task directory')
        _, identity = secure_files.checked_file(path)
        if _identity_dict(identity) != request['frozen_' + key + '_identity']:
            raise ValueError('Frozen input changed after snapshot')
    validate_parameters(request['parameters'])
    return request, directory / 'result'


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _check_active(deadline: float, cancelled) -> None:
    if cancelled():
        raise AudioMasterExecutionStopped('Audio master processing was cancelled; partial output was retained')
    if _remaining(deadline) <= 0:
        raise AudioMasterTimedOut('Audio master processing exceeded the 30 minute execution deadline; partial output was retained')


def _run_process(args: list[str], *, deadline: float, cancelled, label: str,
                 capture_stdout: bool = False) -> subprocess.CompletedProcess:
    _check_active(deadline, cancelled)
    stdout_file = tempfile.TemporaryFile() if capture_stdout else None
    try:
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(args, stdout=stdout_file or subprocess.DEVNULL, stderr=stderr_file,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as exc:
        if stdout_file:
            stdout_file.close()
        raise ValueError(f'FFmpeg {label} unavailable') from exc
    while process.poll() is None:
        try:
            _check_active(deadline, cancelled)
        except (AudioMasterExecutionStopped, AudioMasterTimedOut):
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            stderr_file.close()
            if stdout_file:
                stdout_file.close()
            raise
        time.sleep(min(.05, max(.005, _remaining(deadline) / 20)))
    process.wait()
    stderr_file.seek(0)
    stderr = stderr_file.read().decode('utf-8', errors='replace')
    stderr_file.close()
    stdout = ''
    if stdout_file:
        stdout_file.seek(0)
        stdout = stdout_file.read().decode('utf-8', errors='replace')
        stdout_file.close()
    if process.returncode:
        detail = (stderr or stdout or '').strip()[-1000:]
        raise RuntimeError(f'FFmpeg {label} failed: {detail}')
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _run_ffmpeg(args: list[str], *, deadline: float, cancelled, label: str) -> subprocess.CompletedProcess:
    return _run_process(args, deadline=deadline, cancelled=cancelled, label=label)


def _pcm_source(source: str | Path, *, decode: bool, deadline: float, cancelled) -> Path:
    path = Path(source)
    if path.suffix.lower() == '.wav':
        return path
    target = path.with_name('decoded-' + path.stem + '.wav')
    if decode:
        _run_ffmpeg([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-n', '-i', str(path),
                     '-vn', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(target)],
                    deadline=deadline, cancelled=cancelled, label='audio decode')
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


def _parse_metric(value, *, name: str) -> float:
    if isinstance(value, (int, float)):
        result = float(value)
        if math.isfinite(result):
            return result
        raise ValueError(f'FFmpeg loudnorm field {name} is non-finite')
    if not isinstance(value, str):
        raise ValueError(f'FFmpeg loudnorm field {name} is missing or invalid')
    normalized = value.strip().lower()
    if normalized in {'-inf', '-infinity'}:
        return -120.0
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f'FFmpeg loudnorm field {name} is missing or invalid') from exc
    if not math.isfinite(result):
        raise ValueError(f'FFmpeg loudnorm field {name} is non-finite')
    return result


def _metric_is_explicit_silence(value) -> bool:
    return isinstance(value, str) and value.strip().lower() in {'-inf', '-infinity'}


def _loudnorm_json(stderr: str) -> dict:
    matches = list(re.finditer(r'\{[^{}]*\}', stderr, flags=re.DOTALL))
    for match in reversed(matches):
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and any(key in value for key in ('input_i', 'input_lra', 'input_tp', 'output_i', 'output_tp')):
            return value
    if matches or '{' in stderr:
        raise RuntimeError('FFmpeg loudnorm returned invalid JSON')
    raise RuntimeError('FFmpeg loudnorm did not return a JSON measurement')


def _measure_loudness(path: Path, *, deadline: float, cancelled) -> dict[str, object]:
    result = _run_ffmpeg([str(media_operations.FFMPEG), '-nostdin', '-hide_banner', '-v', 'info', '-i', str(path),
                          '-af', 'loudnorm=I=-16:TP=-1:LRA=11:print_format=json', '-f', 'null', '-'],
                         deadline=deadline, cancelled=cancelled, label='loudness measurement')
    measured = _loudnorm_json(result.stderr)
    fields = {
        'input_i': 'loudness_lufs_i', 'input_lra': 'loudness_lra', 'input_tp': 'true_peak_dbtp',
        'output_i': 'normalization_output_lufs_i', 'output_tp': 'normalization_output_true_peak_dbtp',
    }
    values = {name: _parse_metric(measured.get(name), name=name) for name in fields}
    return {
        'loudness_lufs_i': values['input_i'],
        'loudness_lra': max(0.0, values['input_lra']),
        'true_peak_dbtp': values['input_tp'],
        'normalization_output_lufs_i': values['output_i'],
        'normalization_output_true_peak_dbtp': values['output_tp'],
        'silence_metrics': [name for name in fields if _metric_is_explicit_silence(measured.get(name))],
    }


def _duck_background(source_chunk: np.ndarray, background_chunk: np.ndarray, *, rate: int,
                     duck: dict, state: float, background_gain: float) -> tuple[np.ndarray, float]:
    """Apply a sample-accurate ducking envelope with real attack/release times."""
    detector = np.max(np.abs(source_chunk), axis=1)
    target = 20 * np.log10(np.maximum(detector, 1e-12))
    target = np.maximum(0.0, target - duck['threshold_db']) * (1 - 1 / duck['ratio'])
    attack = 1 - math.exp(-1 / (duck['attack_seconds'] * rate))
    release = 1 - math.exp(-1 / (duck['release_seconds'] * rate))
    envelope = np.empty_like(target)
    for index, wanted in enumerate(target):
        coefficient = attack if wanted > state else release
        state += coefficient * (float(wanted) - state)
        envelope[index] = state
    return background_chunk * (background_gain * np.power(10.0, -envelope[:, None] / 20.0)), state


def execute(payload: dict, cancelled=lambda: False, *, timeout_seconds: float = EXECUTION_TIMEOUT_SECONDS) -> dict:
    request, output_dir = frozen_request(payload)
    if cancelled():
        raise AudioMasterStoppedBeforeStart('Audio master processing stopped before execution')
    timeout_seconds = _db(timeout_seconds, 'timeout_seconds', .001, EXECUTION_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout_seconds
    output_dir.mkdir(exist_ok=False)
    source_path = _pcm_source(request['source'], decode=True, deadline=deadline, cancelled=cancelled)
    background_path = (_pcm_source(request['background'], decode=True, deadline=deadline, cancelled=cancelled)
                       if request['background'] else None)
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
            raise ValueError('Background sample rate and channels must match the normalized source')
        if bg_total < frames and not params['pad_silence']:
            src.close(); bg.close()
            raise ValueError('Background is shorter than the selected master; set pad_silence explicitly to allow it')
    mix_path = output_dir / 'mix.wav'
    duck_state = 0.0
    try:
        with mix_path.open('xb') as handle:
            with wave.open(handle, 'wb') as mix:
                mix.setnchannels(channels); mix.setsampwidth(2); mix.setframerate(rate)
                source_peak = 0.0; clipped = 0
                bg_gain = 10 ** (params['background_gain_db'] / 20)
                duck = params['ducking']
                for index, source_chunk in enumerate(_iter_segment(src, start, min(frames, available))):
                    _check_active(deadline, cancelled)
                    chunk_start = index * CHUNK_FRAMES
                    count = len(source_chunk)
                    if bg is not None:
                        if chunk_start < bg_total:
                            take = min(count, bg_total - chunk_start)
                            background_chunk = next(_iter_segment(bg, chunk_start, take))
                            if take < count:
                                background_chunk = np.pad(background_chunk, ((0, count - take), (0, 0)))
                        else:
                            background_chunk = np.zeros_like(source_chunk)
                        if duck is not None:
                            background_chunk, duck_state = _duck_background(
                                source_chunk, background_chunk, rate=rate, duck=duck,
                                state=duck_state, background_gain=bg_gain)
                        else:
                            background_chunk *= bg_gain
                        source_chunk = source_chunk + background_chunk
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
                    _check_active(deadline, cancelled)
                    zeros = np.zeros((frames - available, channels), dtype='<i2')
                    mix.writeframes(zeros.tobytes())
    finally:
        src.close()
        if bg:
            bg.close()
    _check_active(deadline, cancelled)
    mixed_measurement = _measure_loudness(mix_path, deadline=deadline, cancelled=cancelled)
    output = output_dir / 'master.wav'
    target = params['target_lufs']; true_peak = params['true_peak_dbtp']
    _run_ffmpeg([str(media_operations.FFMPEG), '-nostdin', '-hide_banner', '-v', 'info', '-y', '-i', str(mix_path),
                 '-af', f'loudnorm=I={target}:TP={true_peak}:LRA=11:print_format=json',
                 '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', str(output)],
                deadline=deadline, cancelled=cancelled, label='loudness normalization')
    final_measurement = _measure_loudness(output, deadline=deadline, cancelled=cancelled)
    output_peak = final_measurement['true_peak_dbtp']
    _check_active(deadline, cancelled)
    ffmpeg_version = _ffmpeg_version(deadline=deadline, cancelled=cancelled)
    report = {
        'output': str(output), 'sample_rate': rate, 'channels': channels, 'frames': frames,
        'duration_seconds': frames / rate, 'encoding': 'PCM_16', 'input_peak': source_peak,
        'input_peak_dbtp': mixed_measurement['true_peak_dbtp'],
        'output_true_peak_dbtp': output_peak,
        'loudness_lufs_i': final_measurement['loudness_lufs_i'],
        'loudness_lra': final_measurement['loudness_lra'],
        'clipped_samples': clipped, 'gain_db_applied': final_measurement['loudness_lufs_i'] - mixed_measurement['loudness_lufs_i'],
        'parameters': params, 'ffmpeg_version': ffmpeg_version,
        'input_normalization': '48kHz stereo PCM16 via FFmpeg' if Path(request['source']).suffix.lower() != '.wav' else 'source PCM16 WAV preserved',
        'loudness_silence_metrics': sorted(set(mixed_measurement['silence_metrics'] + final_measurement['silence_metrics'])),
        'true_peak_target_dbtp': true_peak,
        'true_peak_limit_met': output_peak <= true_peak,
        'normalization_warning': None if output_peak <= true_peak else 'Measured output true peak exceeds target; review before adoption',
    }
    receipt = {**request, 'status': 'succeeded', 'output': str(output), 'sample_rate': rate,
               'channels': channels, 'frames': frames, 'duration_seconds': frames / rate,
               'encoding': 'PCM_16', 'report': report}
    temporary = output_dir / 'receipt.tmp'
    _check_active(deadline, cancelled)
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    _check_active(deadline, cancelled)
    temporary.replace(output_dir / 'receipt.json')
    _check_active(deadline, cancelled)
    result = read_receipt(payload)
    _check_active(deadline, cancelled)
    return result


def _finite_number(value, *, low=None, high=None) -> bool:
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _ffmpeg_version(*, deadline: float, cancelled) -> str:
    try:
        process = _run_process([str(media_operations.FFMPEG), '-version'], deadline=deadline, cancelled=cancelled,
                               label='version query', capture_stdout=True)
    except (OSError, AttributeError, ValueError):
        return 'unavailable'
    version = process.stdout or ''
    return (version.splitlines()[0] if version else '').strip() or 'unavailable'


def _read_info(path: Path):
    stream = wave.open(str(path), 'rb')
    rate, channels, total = stream.getframerate(), stream.getnchannels(), stream.getnframes()
    if stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE' or channels not in (1, 2):
        stream.close()
        raise ValueError('Only PCM16 mono/stereo WAV is supported')
    return stream, rate, channels, total


def read_receipt(payload: dict) -> dict:
    request, output_dir = frozen_request(payload)
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('No successful audio master receipt')
    if any(receipt.get(key) != value for key, value in request.items()):
        raise ValueError('Receipt does not match frozen request')
    output = output_dir / 'master.wav'
    output_path, output_identity = secure_files.checked_file(output)
    if receipt.get('output') != str(output) or output_path.is_symlink() or _identity_dict(output_identity)['size'] <= 44:
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
    duration = receipt.get('duration_seconds')
    if not _finite_number(duration, low=0) or abs(duration - frames / rate) > 1 / rate:
        raise ValueError('Audio master receipt duration mismatch')
    report = receipt['report']
    required = {'output', 'sample_rate', 'channels', 'frames', 'duration_seconds', 'input_peak',
                'input_peak_dbtp', 'output_true_peak_dbtp', 'loudness_lufs_i', 'loudness_lra',
                'clipped_samples', 'parameters', 'ffmpeg_version', 'true_peak_target_dbtp',
                'true_peak_limit_met', 'normalization_warning', 'loudness_silence_metrics'}
    if not isinstance(report, dict) or not required <= set(report):
        raise ValueError('Audio master report is incomplete')
    if any(report.get(key) != receipt[key] for key in ('output', 'sample_rate', 'channels', 'frames', 'duration_seconds')):
        raise ValueError('Audio master execution report mismatch')
    numeric_ranges = {
        'input_peak': (0, None), 'input_peak_dbtp': (-120, 20), 'output_true_peak_dbtp': (-120, 20),
        'loudness_lufs_i': (-120, 0), 'loudness_lra': (0, 100), 'true_peak_target_dbtp': (-3, 0),
    }
    if any(not _finite_number(report.get(key), low=low, high=high) for key, (low, high) in numeric_ranges.items()):
        raise ValueError('Audio master report contains invalid numeric values')
    if type(report.get('clipped_samples')) is not int or report['clipped_samples'] < 0:
        raise ValueError('Audio master clipping count is invalid')
    if report['parameters'] != validate_parameters(request['parameters']):
        raise ValueError('Audio master report parameters mismatch')
    if type(report['true_peak_limit_met']) is not bool or (report['normalization_warning'] is not None and not isinstance(report['normalization_warning'], str)):
        raise ValueError('Audio master true peak report is invalid')
    if report['true_peak_limit_met'] != (report['output_true_peak_dbtp'] <= report['true_peak_target_dbtp']):
        raise ValueError('Audio master true peak report contradicts measured output')
    if (report['true_peak_limit_met'] and report['normalization_warning'] is not None) or (not report['true_peak_limit_met'] and not report['normalization_warning']):
        raise ValueError('Audio master true peak warning is inconsistent')
    if (not isinstance(report['loudness_silence_metrics'], list)
            or any(not isinstance(item, str) or item not in {'input_i', 'input_lra', 'input_tp', 'output_i', 'output_tp'}
                   for item in report['loudness_silence_metrics'])):
        raise ValueError('Audio master loudness silence metadata is invalid')
    return receipt
