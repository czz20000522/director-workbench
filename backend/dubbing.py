"""Post-render anchor dubbing.

One task loads IndexTTS once, renders every explicitly timed cue, then mixes the
cues onto the rendered video's existing track (kept at an explicit gain or
replaced with silence).  It never splits, transcribes or adopts anything: the
result is registered as a video candidate plus a complete audio track candidate.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable

from . import creative_inspection, emotion, media_operations, speech

import paths

ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_dub.py'
VOXCPM_ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_dub_voxcpm.py'
TTS_ROOT = speech.TTS_ROOT
MODEL_ROOT = speech.MODEL_ROOT
# 每个引擎一套执行器；能力探测会报告实际解析到的路径，不静默换引擎。
ENGINES = {
    'voxcpm-2': {'label': 'VoxCPM2', 'python': paths.VOXCPM_ROOT / '.venv/Scripts/python.exe', 'adapter': VOXCPM_ADAPTER,
                 'timbre': 'design-or-reference'},
    'indextts-2.5': {'label': 'IndexTTS 2.5', 'python': TTS_ROOT / '.venv/Scripts/python.exe', 'adapter': ADAPTER,
                     'timbre': 'reference-only'},
}
DEFAULT_ENGINE = 'voxcpm-2'
EMOTION_MODES = {'indextts-2.5': None, 'voxcpm-2': {'none': True, 'reference': False, 'text': True, 'vector': False}}
CUE_LIMIT = 24
MAX_TEXT = 500
MAX_SOURCE_BYTES = 2 * 1024**3
RUN_TIMEOUT = 1800
VIDEO_SUFFIXES = {'.mp4', '.mov', '.webm', '.mkv'}
REFERENCE_SUFFIXES = {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg'}


class DubbingFailed(RuntimeError):
    pass


class DubbingStopped(RuntimeError):
    pass


def capability(engine: str = 'indextts-2.5') -> dict[str, Any]:
    """Report one dubbing engine's files; a missing file is never swapped for another engine."""
    if engine not in ENGINES:
        raise ValueError('未知配音引擎')
    spec = ENGINES[engine]
    if engine == 'voxcpm-2':
        models = paths.VOXCPM_ROOT / 'pretrained_models/VoxCPM2'
        required = {'配音执行器': VOXCPM_ADAPTER, 'VoxCPM Python 环境': spec['python'],
                    'VoxCPM 程序': paths.VOXCPM_ROOT / 'src/voxcpm/core.py',
                    'VoxCPM2 主模型': models / 'model.safetensors', 'VoxCPM2 配置': models / 'config.json',
                    'VoxCPM2 声码器': models / 'audiovae.pth'}
        missing = [label for label, path in required.items() if not path.is_file()]
        modes = dict(EMOTION_MODES[engine])
        timbres = ['design', 'reference']
    else:
        readiness = speech.capability('indextts-2.5')
        missing = list(readiness.get('missing') or [])
        modes = dict(readiness.get('emotion_modes') or {})
        timbres = ['reference']
    if not media_operations.FFMPEG.is_file():
        missing.append('FFmpeg')
    return {'available': not missing, 'missing': missing, 'engine': spec['label'], 'engine_id': engine,
            'id': 'anchor-dubbing', 'modes': timbres, 'timbre': spec['timbre'], 'emotion_modes': modes,
            'emotion_unsupported': ['reference', 'vector'] if engine == 'voxcpm-2' else [],
            'root': str(paths.VOXCPM_ROOT if engine == 'voxcpm-2' else TTS_ROOT), 'language': 'ZH',
            'note': '按显式时间点摆放台词；原片音轨保留或静音；不会自动拆分或采用'}


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
        raise ValueError('配音任务 ID 无效')
    if task_id.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('配音任务 ID 是保留名称')


def _validate_cues(cues: list[dict], duration: float) -> list[dict]:
    if not isinstance(cues, list) or not 1 <= len(cues) <= CUE_LIMIT:
        raise ValueError(f'配音台词须为 1 至 {CUE_LIMIT} 条')
    normalized = []
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict) or set(cue) != {'at_seconds', 'text', 'gain'}:
            raise ValueError('每条台词只接受 at_seconds、text 与 gain')
        at, text, gain = cue['at_seconds'], cue['text'], cue['gain']
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError('台词开始时间必须是非负有限数值')
        if at >= duration:
            raise ValueError(f'第 {index} 条台词开始时间超出视频时长（{duration:.2f} 秒）')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f'台词长度须为 1 至 {MAX_TEXT} 字符')
        if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 4:
            raise ValueError('单句音量须在 0 至 4 之间')
        normalized.append({'at_seconds': float(at), 'text': text, 'gain': float(gain)})
    return normalized


def _engine_emotion(engine: str, emotion_spec: dict | None) -> dict:
    """VoxCPM has no emotion vector/reference blend, so modes are checked per engine."""
    ready = (speech.MODEL_ROOT / 'qwen0.6bemo4-merge' / 'config.json').is_file() if engine == 'indextts-2.5' else True
    frozen = emotion.validate(emotion_spec, text_engine_ready=ready)
    allowed = EMOTION_MODES[engine] if EMOTION_MODES[engine] else {'none': True, 'reference': True, 'text': ready, 'vector': True}
    if not allowed.get(frozen['mode']):
        raise ValueError(f'{ENGINES[engine]["label"]} 不支持这种情绪控制方式，请改用文本描述或不控制情绪')
    return frozen


def prepare(task_id: str, source: Path, reference: Path | None, cues: list[dict], snapshot_root: Path, *,
            original_audio: str, original_gain: float, emotion_spec: dict | None = None,
            engine: str = DEFAULT_ENGINE, seed: int = 20261025, inference_timesteps: int = 10,
            cfg_value: float = 2.0, voice_description: str | None = None) -> dict:
    """Freeze every input before the task is queued; callers authorize all paths."""
    _validate_task_id(task_id)
    if engine not in ENGINES:
        raise ValueError('未知配音引擎')
    frozen_emotion = _engine_emotion(engine, emotion_spec)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError('随机编号须为 32 位非负整数')
    if type(inference_timesteps) is not int or not 1 <= inference_timesteps <= 60:
        raise ValueError('推理步数须为 1 至 60 的整数')
    if type(cfg_value) not in (int, float) or not math.isfinite(cfg_value) or not 0 <= cfg_value <= 20:
        raise ValueError('引导系数须在 0 至 20 之间')
    if engine == 'indextts-2.5':
        if reference is None or voice_description is not None:
            raise ValueError('IndexTTS 只能沿用参考音色：必须提供音色参考，且不提供音色描述')
    elif bool(reference is None) == (voice_description is None):
        raise ValueError('VoxCPM2 配音要么提供参考音色，要么给出音色文字描述，不能两者同给或同缺')
    if voice_description is not None and (not isinstance(voice_description, str) or not voice_description.strip()
                                          or len(voice_description) > 2000):
        raise ValueError('音色描述须为 1 至 2000 字符')
    source = Path(source).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError('配音源必须是已生成的视频文件')
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('首版锚点配音限制单个视频不超过 2 GiB')
    if reference is not None:
        reference = Path(reference).resolve(strict=True)
        if not reference.is_file() or reference.suffix.lower() not in REFERENCE_SUFFIXES:
            raise ValueError('音色参考必须是可读取的音频文件')
    facts = creative_inspection.probe(source)
    duration = facts.get('duration_seconds')
    if not facts.get('known') or facts.get('width') is None or facts.get('height') is None \
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('无法读取源视频的时长或画面轨')
    frozen_cues = _validate_cues(cues, float(duration))
    if original_audio not in {'keep', 'mute'}:
        raise ValueError('原片音轨只能选择保留或静音')
    if type(original_gain) not in (int, float) or not math.isfinite(original_gain) or not 0 <= original_gain <= 3:
        raise ValueError('原片音量须在 0 至 3 之间')

    root = Path(snapshot_root)
    if not root.is_absolute():
        raise ValueError('配音任务根目录必须是绝对路径')
    directory = root.resolve() / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen_source = directory / ('source' + source.suffix.lower())
    shutil.copyfile(source, frozen_source)
    frozen_reference = None
    if reference is not None:
        frozen_reference = directory / ('voice-reference' + reference.suffix.lower())
        shutil.copyfile(reference, frozen_reference)
    request = {'task_id': task_id, 'kind': 'anchor_dubbing', 'engine': engine,
               'source': str(frozen_source), 'voice_reference': str(frozen_reference) if frozen_reference else None,
               'voice_description': voice_description.strip() if voice_description else None,
               'cues': frozen_cues, 'original_audio': original_audio,
               'original_gain': float(original_gain),
               'emotion': emotion.freeze(frozen_emotion, directory) if frozen_emotion['mode'] == 'reference' else frozen_emotion,
               'seed': seed, 'inference_timesteps': inference_timesteps, 'cfg_value': float(cfg_value),
               'source_duration_seconds': float(duration),
               'source_has_audio': bool(facts.get('audio_streams'))}
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'anchor_dubbing', 'request': request, 'request_path': str(request_path),
            'output_dir': str(directory / 'result'), 'engine': engine}


def frozen_request(payload: dict) -> tuple[dict, Path]:
    request = payload.get('request')
    request_path = Path(str(payload.get('request_path') or ''))
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('配音冻结请求无效')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('配音请求快照与任务不一致')
    expected = {'task_id', 'kind', 'engine', 'source', 'voice_reference', 'voice_description', 'cues',
                'original_audio', 'original_gain', 'emotion', 'seed', 'inference_timesteps', 'cfg_value',
                'source_duration_seconds', 'source_has_audio'}
    if request['engine'] not in ENGINES:
        raise ValueError('配音冻结请求引擎无效')
    if set(request) != expected or request['kind'] != 'anchor_dubbing':
        raise ValueError('配音冻结请求字段无效')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name:
        raise ValueError('配音任务目录不一致')
    if Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('配音输出目录不一致')
    inputs = [('source', True), ('voice_reference', False)]
    for key, required in inputs:
        value = request[key]
        if value is None:
            if required:
                raise ValueError('配音输入不在冻结任务目录内')
            continue
        path = Path(str(value))
        if not path.is_absolute() or path.parent.resolve() != directory or not path.is_file():
            raise ValueError('配音输入不在冻结任务目录内')
    _validate_cues(request['cues'], float(request['source_duration_seconds']))
    return request, directory / 'result'


def _wave_duration(path: Path) -> float:
    with wave.open(str(path), 'rb') as stream:
        frames, rate = stream.getnframes(), stream.getframerate()
    if frames <= 0 or rate <= 0:
        raise ValueError(f'配音音频无效: {path.name}')
    return frames / rate


def _run_adapter(payload: dict, directory: Path, cancelled: Callable[[], bool]) -> dict:
    speech_dir = directory / 'speech'
    spec = ENGINES[payload['request']['engine']]
    command = [str(spec['python']), '-u', str(spec['adapter']),
               '--request', str(directory / 'request.json'), '--output-dir', str(speech_dir)]
    log_path = directory / 'execution.log'
    with log_path.open('ab') as log:
        try:
            process = subprocess.Popen(command, cwd=Path(spec['python']).parents[2], stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError as exc:
            raise DubbingFailed(f'配音执行器无法启动: {exc}') from exc
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled():
                    raise DubbingStopped('配音任务已停止')
                if time.monotonic() - started >= RUN_TIMEOUT:
                    raise TimeoutError('配音生成超时')
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException as exc:
            speech.stop_owned_process(process)
            if isinstance(exc, TimeoutError):
                raise DubbingFailed('配音生成超时，已停止本次进程') from exc
            raise
    if process.returncode != 0:
        raise DubbingFailed(f'配音执行器退出码 {process.returncode}，详情见任务 execution.log')
    receipt_path = speech_dir / 'receipt.json'
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise DubbingFailed('配音执行器没有留下有效回执') from exc
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded' or receipt.get('task_id') != payload['request']['task_id']:
        raise DubbingFailed('配音执行器回执与任务不一致')
    if len(receipt.get('cues') or []) != len(payload['request']['cues']):
        raise DubbingFailed('配音执行器回执缺少台词')
    return receipt


def _mix(request: dict, speech_receipt: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / 'dubbed.mp4'
    audio = output_dir / 'dubbed-audio.wav'
    duration = float(request['source_duration_seconds'])
    frame = f'{duration:.3f}'
    command = [str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y', '-i', request['source']]
    speech_cues = speech_receipt['cues']
    for cue in speech_cues:
        command += ['-i', cue['output']]

    graph = []
    if request['original_audio'] == 'keep' and request['source_has_audio']:
        graph.append(f"[0:a:0]aresample=48000,aformat=channel_layouts=stereo,volume={request['original_gain']}[base]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{frame}[base]")
    labels = ['[base]']
    for position, cue in enumerate(speech_cues, start=1):
        delay = round(float(cue['at_seconds']) * 1000)
        gain = request['cues'][position - 1]['gain']
        graph.append(f"[{position}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                     f"adelay={delay}|{delay},volume={gain}[cue{position}]")
        labels.append(f'[cue{position}]')
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
                 f"alimiter=limit=0.95[aout]")
    command += ['-filter_complex', ';'.join(graph), '-map', '0:v:0', '-map', '[aout]',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', frame,
                '-movflags', '+faststart', str(video)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=300,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y', '-i', str(video),
                        '-vn', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(audio)],
                       check=True, capture_output=True, timeout=300,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise DubbingFailed(f'配音混音失败: {exc}') from exc
    facts = creative_inspection.probe(video)
    if not facts.get('known') or facts.get('width') is None or not (facts.get('audio_streams') or 0):
        raise DubbingFailed('混音结果缺少画面或音轨')
    produced = facts.get('duration_seconds')
    if type(produced) not in (int, float) or not math.isfinite(produced) or abs(float(produced) - duration) > 0.25:
        raise DubbingFailed('混音结果时长与源视频不一致')
    audio_duration = _wave_duration(audio)
    if abs(audio_duration - duration) > 0.25:
        raise DubbingFailed('配音音轨时长与源视频不一致')
    timeline = []
    for position, cue in enumerate(speech_cues):
        start = float(cue['at_seconds'])
        end = start + float(cue['duration_seconds'])
        timeline.append({'index': cue['index'], 'at_seconds': start, 'text': cue['text'],
                         'duration_seconds': cue['duration_seconds'],
                         'end_seconds': end, 'truncated': end > duration + 0.05,
                         'gain': request['cues'][position]['gain']})
    return {'video': str(video.resolve()), 'audio': str(audio.resolve()),
            'duration_seconds': float(produced), 'audio_duration_seconds': audio_duration,
            'width': facts.get('width'), 'height': facts.get('height'), 'fps': facts.get('fps'),
            'timeline': timeline}


def execute(payload: dict, cancelled: Callable[[], bool] = lambda: False) -> dict:
    request, output_dir = frozen_request(payload)
    if cancelled():
        raise DubbingStopped('配音任务在启动前停止')
    speech_receipt = _run_adapter(payload, output_dir.parent, cancelled)
    mixed = _mix(request, speech_receipt, output_dir)
    receipt = {**request, 'status': 'succeeded', 'output': mixed['video'],
               'audio_output': mixed['audio'], 'duration_seconds': mixed['duration_seconds'],
               'audio_duration_seconds': mixed['audio_duration_seconds'],
               'width': mixed['width'], 'height': mixed['height'], 'fps': mixed['fps'],
               'timeline': mixed['timeline']}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return read_receipt(payload)


def read_receipt(payload: dict) -> dict:
    request, output_dir = frozen_request(payload)
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('没有成功的配音回执')
    if any(receipt.get(key) != value for key, value in request.items()):
        raise ValueError('配音回执与冻结请求不一致')
    video = output_dir / 'dubbed.mp4'
    audio = output_dir / 'dubbed-audio.wav'
    if receipt.get('output') != str(video) or receipt.get('audio_output') != str(audio):
        raise ValueError('配音回执输出路径不一致')
    if video.is_symlink() or not video.is_file() or audio.is_symlink() or not audio.is_file():
        raise ValueError('配音输出文件缺失')
    facts = creative_inspection.probe(video)
    if not facts.get('known') or facts.get('width') is None or not (facts.get('audio_streams') or 0):
        raise ValueError('配音视频缺少画面或音轨')
    duration = receipt.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('配音回执时长无效')
    if abs(duration - float(request['source_duration_seconds'])) > 0.25:
        raise ValueError('配音回执时长与源视频不一致')
    if abs(_wave_duration(audio) - duration) > 0.25:
        raise ValueError('配音音轨时长与回执不一致')
    timeline = receipt.get('timeline')
    if not isinstance(timeline, list) or len(timeline) != len(request['cues']):
        raise ValueError('配音回执台词数量不一致')
    for position, (expected, actual) in enumerate(zip(request['cues'], timeline), start=1):
        if not isinstance(actual, dict) or actual.get('at_seconds') != expected['at_seconds'] \
                or actual.get('text') != expected['text'] or actual.get('gain') != expected['gain']:
            raise ValueError(f'配音回执第 {position} 句与冻结台词不一致')
        cue_duration = actual.get('duration_seconds')
        if type(cue_duration) not in (int, float) or not math.isfinite(cue_duration) or cue_duration <= 0:
            raise ValueError(f'配音回执第 {position} 句时长无效')
        if not isinstance(actual.get('truncated'), bool):
            raise ValueError(f'配音回执第 {position} 句截断标记无效')
        if abs(float(actual.get('end_seconds', 0)) - (float(expected['at_seconds']) + float(cue_duration))) > 0.01:
            raise ValueError(f'配音回执第 {position} 句结束时间不一致')
    return receipt
