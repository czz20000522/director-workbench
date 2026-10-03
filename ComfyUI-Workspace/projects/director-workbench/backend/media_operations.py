"""Frozen, bounded CPU media jobs. Only the API supplies authorized source/root paths."""
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_PYTHON = Path('D:/Comfy-Desktop/ComfyUI-Installs/第一个comfyui配置/ComfyUI/.venv/Scripts/python.exe')
ASR_ROOT = Path('D:/Comfy-Desktop/ComfyUI-Shared/tools/index-tts')
ASR_PYTHON = ASR_ROOT / '.venv/Scripts/python.exe'
FFMPEG = ASR_ROOT / 'ffmpeg.exe'
WHISPER_MODEL = ASR_ROOT / 'models/whisper/base.pt'
MAX_BYTES = 2 * 1024 ** 3


class MediaOperationFailed(RuntimeError):
    pass


class MediaOperationStopped(MediaOperationFailed):
    pass


class MediaOperationUncertain(RuntimeError):
    """Worker state is unknown: retain the execution slot until reconciliation."""


def capability():
    operations = {}
    for name, paths in {'silence': [DESKTOP_PYTHON], 'video_qc': [DESKTOP_PYTHON],
                        'silent_video': [DESKTOP_PYTHON, FFMPEG],
                        'transcribe': [ASR_PYTHON, FFMPEG, WHISPER_MODEL]}.items():
        reasons = [f'Missing local runtime: {p}' for p in paths if not p.is_file()]
        operations[name] = {'available': not reasons, 'missing': reasons}
    return {'operations': operations, 'execution': 'local CPU', 'downloads': False}


def _number(value, name, low, high, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    if integer and int(value) != value:
        raise ValueError(f'{name} must be an integer')
    return int(value) if integer else float(value)


def validate_parameters(operation, parameters):
    if not isinstance(parameters, dict):
        raise ValueError('parameters must be an object')
    allowed = {'silence': {'duration_seconds', 'sample_rate', 'channels'},
               'video_qc': {'sample_count'}, 'silent_video': set(),
               'transcribe': {'language', 'word_timestamps', 'expected_text'}}
    if operation not in allowed or set(parameters) - allowed[operation]:
        raise ValueError('Unknown media operation or parameter')
    if operation == 'silence':
        duration = _number(parameters.get('duration_seconds'), 'duration_seconds', 0, 120)
        rate = parameters.get('sample_rate', 24000)
        channels = parameters.get('channels', 1)
        if type(rate) is not int or rate not in (16000, 24000, 48000) or type(channels) is not int or channels not in (1, 2):
            raise ValueError('Unsupported PCM sample rate or channel count')
        if duration <= 0 or round(duration * rate) < 1:
            raise ValueError('Duration must contain at least one PCM sample')
        return dict(duration_seconds=duration, sample_rate=rate, channels=channels)
    if operation == 'video_qc':
        return {'sample_count': _number(parameters.get('sample_count', 12), 'sample_count', 3, 60, True)}
    if operation == 'transcribe':
        language = parameters.get('language', 'zh')
        words = parameters.get('word_timestamps', True)
        expected = parameters.get('expected_text', '')
        if language not in ('zh', 'en', 'auto') or type(words) is not bool or not isinstance(expected, str) or len(expected) > 20000:
            raise ValueError('Invalid transcription parameters')
        return dict(language=language, word_timestamps=words, expected_text=expected)
    return {}


def _plain_path(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise MediaOperationFailed('Media path escapes task directory')
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise MediaOperationFailed('Linked media paths are not permitted')
    if not path.resolve().is_relative_to(root.resolve()):
        raise MediaOperationFailed('Resolved media path escapes task directory')
    return path


def prepare(task_id, operation, source, snapshot_root, parameters):
    parameters = validate_parameters(operation, parameters)
    if not isinstance(task_id, str) or not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('Invalid task ID')
    root = Path(snapshot_root).absolute() / task_id
    _plain_path(root, root)
    if root.exists() and any(root.iterdir()):
        raise ValueError('Task snapshot directory must be empty')
    root.mkdir(parents=True, exist_ok=True)
    output = root / 'result'
    output.mkdir()
    frozen = None
    if operation != 'silence':
        source = Path(source) if source is not None else None
        if source is None or not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise ValueError('A source file of at most 2 GiB is required')
        destination = root / ('source' + source.suffix.lower())
        before = source.stat()
        shutil.copyfile(source, destination)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source changed during snapshot')
        frozen = {'path': str(destination), 'bytes': destination.stat().st_size,
                  'mtime_ns': destination.stat().st_mtime_ns}
    elif source is not None:
        raise ValueError('Silence does not accept a source')
    request = {'task_id': task_id, 'operation': operation, 'parameters': parameters,
               'source': frozen, 'output_dir': str(output)}
    request_path = root / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    return {'kind': 'media_operation', 'operation': operation, 'request': request,
            'request_path': str(request_path), 'output_dir': str(output)}


def _validate_payload(payload):
    request = payload['request']
    request_path = Path(payload['request_path'])
    root = request_path.parent
    if request_path.name != 'request.json' or payload.get('kind') != 'media_operation':
        raise MediaOperationFailed('Invalid frozen request')
    _plain_path(request_path, root)
    if request_path.stat().st_size > 100000 or json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise MediaOperationFailed('Frozen request was modified')
    if request['operation'] != payload['operation'] or str(root / 'result') != request['output_dir'] or payload['output_dir'] != request['output_dir']:
        raise MediaOperationFailed('Inconsistent frozen output directory')
    _plain_path(request['output_dir'], root)
    if validate_parameters(request['operation'], request['parameters']) != request['parameters']:
        raise MediaOperationFailed('Invalid frozen parameters')
    if request['source']:
        source = _plain_path(request['source']['path'], root)
        if source.parent != root or not source.name.startswith('source.'):
            raise MediaOperationFailed('Invalid frozen source path')
        stat = source.stat()
        if stat.st_size != request['source']['bytes'] or stat.st_mtime_ns != request['source']['mtime_ns'] or stat.st_size > MAX_BYTES:
            raise MediaOperationFailed('Frozen source changed')
    return root


def read_receipt(payload):
    try:
        root = _validate_payload(payload)
        path = _plain_path(root / 'result/receipt.json', root)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.stat().st_size > 16 * 1024 ** 2:
            raise MediaOperationFailed('Oversized receipt')
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if any(receipt.get(k) != v for k, v in payload['request'].items()) or receipt.get('status') != 'succeeded':
            raise MediaOperationFailed('Receipt does not match frozen request')
        outputs = receipt.get('outputs')
        if not isinstance(outputs, list) or not outputs or len(outputs) > 65 or not isinstance(receipt.get('report'), dict):
            raise MediaOperationFailed('Invalid media receipt')
        seen = set()
        for entry in outputs:
            item = _plain_path(entry['path'], root / 'result')
            allowed = {'silence': {'silence.wav': 'audio', 'report.json': 'report'},
                       'silent_video': {'silent-review.mp4': 'video', 'report.json': 'report'},
                       'transcribe': {'recognition-input.wav': 'audio', 'transcript.txt': 'report', 'report.json': 'report'},
                       'video_qc': {'contact-sheet.jpg': 'image', 'report.json': 'report'}}[payload['operation']]
            expected_kind = allowed.get(item.name)
            if payload['operation'] == 'video_qc' and re.fullmatch(r'frame-\d{6}\.jpg', item.name):
                expected_kind = 'image'
            if item.parent != root / 'result' or expected_kind != entry.get('kind'):
                raise MediaOperationFailed('Unexpected output artifact name/type')
            if not item.is_file() or item.stat().st_size == 0 or item in seen or entry['kind'] not in ('audio', 'video', 'image', 'report'):
                raise MediaOperationFailed('Invalid output artifact')
            seen.add(item)
            if item.stat().st_size != entry.get('bytes'):
                raise MediaOperationFailed('Output artifact size changed')
            with item.open('rb') as stream:
                header = stream.read(16)
            if entry['kind'] == 'audio' and not (header[:4] == b'RIFF' and header[8:12] == b'WAVE'):
                raise MediaOperationFailed('Invalid WAV output')
            if entry['kind'] == 'video' and header[4:8] != b'ftyp':
                raise MediaOperationFailed('Invalid MP4 output')
            if entry['kind'] == 'image' and not header.startswith(b'\xff\xd8\xff'):
                raise MediaOperationFailed('Invalid JPEG output')
        report_path = _plain_path(root / 'result/report.json', root)
        if report_path not in seen or json.loads(report_path.read_text(encoding='utf-8')) != receipt['report']:
            raise MediaOperationFailed('Report disagrees with receipt')
        if not set(allowed).issubset({item.name for item in seen}):
            raise MediaOperationFailed('Missing required output artifacts')
        return receipt
    except (MediaOperationFailed, FileNotFoundError):
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MediaOperationFailed(f'Invalid media receipt: {exc}') from exc


def _process_identity(pid):
    if os.name != 'nt':
        try:
            return Path(f'/proc/{pid}/stat').read_text().split()[21]
        except FileNotFoundError:
            return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise MediaOperationUncertain('Unable to verify worker identity')
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise MediaOperationUncertain('Unable to verify worker exit status')
        if exit_code.value != 259:  # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise MediaOperationUncertain('Unable to read worker creation time')
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def owner_alive(payload):
    """True/False only when identity is known; missing/ambiguous records raise."""
    root = _validate_payload(payload)
    try:
        path = _plain_path(root / 'worker.json', root)
        owner = json.loads(path.read_text(encoding='utf-8'))
        if not owner.get('creation_identity') or type(owner.get('pid')) is not int:
            raise ValueError('Missing worker creation identity')
        identity = _process_identity(owner['pid'])
        return identity is not None and identity == owner['creation_identity']
    except (OSError, ValueError, KeyError) as exc:
        raise MediaOperationUncertain('Worker status cannot be confirmed') from exc


def _stop(process):
    if process.poll() is None:
        try:
            if os.name == 'nt':
                result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=20)
                if result.returncode:
                    raise MediaOperationUncertain('Unable to confirm worker process-tree termination')
            else:
                process.kill()
            process.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaOperationUncertain('Worker termination is unconfirmed') from exc


def execute(payload, cancelled=lambda: False):
    root = _validate_payload(payload)
    try:
        return read_receipt(payload)
    except FileNotFoundError:
        pass
    if (root / 'worker.json').exists():
        raise MediaOperationUncertain('An execution record already exists; do not replay an unknown worker')
    if cancelled():
        raise MediaOperationStopped('Cancelled before execution')
    python = ASR_PYTHON if payload['operation'] == 'transcribe' else DESKTOP_PYTHON
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PYTHONNOUSERSITE'] = '1'
    env['PATH'] = str(FFMPEG.parent) + os.pathsep + env.get('PATH', '')
    with (root / 'worker.log').open('wb') as log:
        process = subprocess.Popen([str(python), str(ROOT / 'tools/process_media.py'), '--request', str(root / 'request.json')],
                                   stdout=log, stderr=subprocess.STDOUT, env=env,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + (1200 if payload['operation'] == 'transcribe' else 600)
        try:
            (root / 'worker.json').write_text(json.dumps({'pid': process.pid, 'started_at': time.time(), 'creation_identity': _process_identity(process.pid), 'python': str(python)}), encoding='utf-8')
            while process.poll() is None:
                if cancelled():
                    raise MediaOperationStopped('Media operation cancelled')
                if time.monotonic() > deadline:
                    raise MediaOperationFailed('Media operation timed out')
                time.sleep(0.15)
        finally:
            _stop(process)
    if process.returncode:
        raise MediaOperationFailed('Media worker failed; inspect task worker.log')
    receipt = read_receipt(payload)
    if receipt is None:
        raise MediaOperationFailed('Worker exited without a receipt')
    return receipt
