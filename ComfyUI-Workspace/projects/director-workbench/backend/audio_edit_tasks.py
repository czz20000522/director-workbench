"""Frozen CPU audio tasks. Authorization and lifecycle storage belong to the caller."""
from pathlib import Path
import json
import math
import re
import shutil
import wave
import subprocess

from . import audio_edit, creative_inspection, media_operations

DEFAULTS = dict(start_seconds=0, duration_seconds=None, gain=1,
                fade_in_seconds=0, fade_out_seconds=0, background_gain=.25,
                background_offset_seconds=0, pad_silence=False)


class AudioEditStoppedBeforeStart(RuntimeError):
    """No output or processing has started."""


def prepare(task_id: str, source: Path, snapshot_root: Path, *, parameters: dict,
            background: Path | None = None) -> dict:
    """Paths/root have already been authorized by the calling project context."""
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
        raise ValueError('Invalid task ID')
    if task_id.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('Reserved task ID')
    if not isinstance(parameters, dict) or set(parameters) - set(DEFAULTS):
        raise ValueError('Unknown audio editing parameters')
    frozen_parameters = {**DEFAULTS, **parameters}
    # Validate all scalar parameters before reserving a task directory.
    for name, value in frozen_parameters.items():
        if name == 'pad_silence':
            if type(value) is not bool:
                raise ValueError('pad_silence must be boolean')
            continue
        if name == 'duration_seconds' and value is None:
            continue
        audio_edit.number(value, name, high=86400 if name == 'start_seconds' else 8 if name in ('gain', 'background_gain') else 120)
    source = Path(source).resolve(strict=True)
    background = None if background is None else Path(background).resolve(strict=True)
    for path in (source, background):
        if path is not None:
            if not path.is_file():
                raise ValueError('Audio input must be a file')
            if path.suffix.lower() == '.mp3':
                facts = creative_inspection.probe(path)
                if not facts.get('audio_streams') or not 0 < (facts.get('duration_seconds') or 0) <= 120:
                    raise ValueError('MP3 source must have a readable audio track of at most 120 seconds')
                if not media_operations.FFMPEG.is_file():
                    raise ValueError('Local CPU audio decoder is unavailable')
            else:
                with audio_edit.pcm_input(path):
                    pass
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
    request = {'task_id': task_id, 'kind': 'audio_edit', 'source': str(frozen_source),
               'background': None if frozen_background is None else str(frozen_background),
               'parameters': frozen_parameters}
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'audio_edit', 'request': request, 'request_path': str(request_path),
            'output_dir': str(directory / 'result')}


def frozen_request(payload: dict) -> tuple[dict, Path]:
    request = payload['request']
    request_path = Path(payload['request_path'])
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('Invalid frozen task payload')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('Request snapshot differs from stored task')
    if set(request) != {'task_id', 'kind', 'source', 'background', 'parameters'} or request['kind'] != 'audio_edit':
        raise ValueError('Invalid frozen request schema')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name or Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('Frozen task directory mismatch')
    for key, name in (('source', 'source.wav'), ('background', 'background.wav')):
        if key == 'background' and request[key] is None:
            continue
        path = Path(request[key])
        if path.suffix.lower() not in {'.wav', '.mp3'} or path.resolve() != directory / (key + path.suffix.lower()):
            raise ValueError('Input is outside the frozen task directory')
    return request, directory / 'result'


def pcm_source(source, *, decode=False):
    if source is None or Path(source).suffix.lower() == '.wav':
        return source
    source = Path(source)
    target = source.with_name('decoded-' + source.stem + '.wav')
    if decode:
        subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-n', '-i', str(source),
                        '-vn', '-t', '120.01', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(target)],
                       check=True, timeout=180, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if target.is_symlink() or not target.is_file():
        raise ValueError('Normalized audio input unavailable')
    return str(target)


def execute(payload: dict, cancelled=lambda: False) -> dict:
    """Cancellation is only observed BEFORE execution; no false mid-run stop claim.

    Caller may prevent adopting a completed result if a late stop was requested.
    Interrupted/partial output is retained; never automatically retry this directory.
    """
    request, output_dir = frozen_request(payload)
    if cancelled():
        raise AudioEditStoppedBeforeStart('Audio edit stopped before execution')
    output_dir.mkdir(exist_ok=False)
    report = audio_edit.edit_audio(pcm_source(request['source'], decode=True), output_dir / 'speech.wav',
                                  background=pcm_source(request['background'], decode=True), output_root=output_dir,
                                  **request['parameters'])
    receipt = {**request, 'status': 'succeeded', 'output': report['output'],
               'sample_rate': report['sample_rate'], 'channels': report['channels'],
               'frames': report['frames'], 'duration_seconds': report['duration_seconds'],
               'encoding': report['encoding'], 'report': report}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return read_receipt(payload)


def read_receipt(payload: dict) -> dict:
    request, output_dir = frozen_request(payload)
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('No successful audio edit receipt')
    if any(receipt.get(key) != value for key, value in request.items()):
        raise ValueError('Receipt does not match frozen request')
    output = output_dir / 'speech.wav'
    if receipt.get('output') != str(output) or output.is_symlink() or output.resolve() != output:
        raise ValueError('Unexpected output path')
    with audio_edit.pcm_input(output) as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        if frames <= 0 or frames > rate * 120:
            raise ValueError('Invalid output frame count')
        remaining = frames
        while remaining:
            count = min(remaining, 65536)
            if len(stream.readframes(count)) != count * channels * 2:
                raise ValueError('Truncated output audio')
            remaining -= count
    expected = dict(sample_rate=rate, channels=channels, frames=frames, encoding='PCM_16')
    with audio_edit.pcm_input(pcm_source(request['source'])) as source:
        p = request['parameters']
        expected_frames = (source.getnframes() - round(p['start_seconds'] * source.getframerate())
                           if p['duration_seconds'] is None else round(p['duration_seconds'] * source.getframerate()))
        if (rate, channels, frames) != (source.getframerate(), source.getnchannels(), expected_frames):
            raise ValueError('Output does not match requested selection and format')
    if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('Receipt audio metadata mismatch')
    duration = receipt.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or abs(duration - frames / rate) > 1 / rate:
        raise ValueError('Receipt duration mismatch')
    report = receipt.get('report')
    if not isinstance(report, dict) or any(report.get(key) != receipt[key] for key in ('output', 'sample_rate', 'channels', 'frames', 'duration_seconds', 'encoding')):
        raise ValueError('Execution report mismatch')
    return receipt
