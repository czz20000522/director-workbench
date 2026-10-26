"""成片后单独改音频：画面流原样复制，音轨换成新的配乐或保留原声叠加。

这是「生视频之后音频仍要能单独改」的那条链路：不重新渲染画面，不碰 GPU，
FFmpeg 只做音轨（`-c:v copy`）。它和锚点配音的区别是**不需要台词**——只是把
项目里的某段音频铺到这条视频上，或者干脆静音。
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable

from . import creative_inspection, media_operations

SAMPLE_RATE = 48000
MAX_RUN_SECONDS = 600
MAX_FADE = 10.0
VIDEO_SUFFIXES = {'.mp4', '.mov', '.webm', '.mkv'}
AUDIO_SUFFIXES = {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg'}


class AudioSwapFailed(RuntimeError):
    pass


class AudioSwapStopped(RuntimeError):
    pass


def capability() -> dict[str, Any]:
    ready = media_operations.FFMPEG.is_file()
    return {'available': ready, 'missing': [] if ready else ['FFmpeg'],
            'engine': 'FFmpeg（只换音轨，不重渲染画面）',
            'note': '画面流直接复制；原声可保留并调音量，也可静音；可叠加一段音频素材'}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}须在 {low} 到 {high} 之间')
    return float(value)


def validate(request: dict[str, Any], source_facts: dict[str, Any], overlay_is_audio: bool) -> dict[str, Any]:
    """Normalize the frozen request; every number is explicit, nothing is inferred."""
    if set(request) != {'original_audio', 'original_gain', 'overlay_gain', 'start_seconds',
                        'overlay_loop', 'fade_in_seconds', 'fade_out_seconds'}:
        raise ValueError('换音轨参数字段无效')
    if not source_facts.get('known') or source_facts.get('width') is None:
        raise ValueError('换音轨的源文件必须是有画面的视频')
    duration = source_facts.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('源视频时长未知，无法换音轨')
    has_audio = bool(source_facts.get('audio_streams'))
    if request['original_audio'] not in {'keep', 'mute'}:
        raise ValueError('原声只能是保留或静音')
    if request['original_audio'] == 'keep' and not has_audio:
        raise ValueError('这条视频本来就没有音轨，原声只能选静音')
    original_gain = _number(request['original_gain'], '原声音量', 0, 3)
    overlay_gain = _number(request['overlay_gain'], '叠加音量', 0, 3)
    start = _number(request['start_seconds'], '叠加开始时间', 0, float(duration))
    if type(request['overlay_loop']) is not bool:
        raise ValueError('叠加音频的循环方式无效')
    fade_in = _number(request['fade_in_seconds'], '淡入时长', 0, min(MAX_FADE, float(duration)))
    fade_out = _number(request['fade_out_seconds'], '淡出时长', 0, min(MAX_FADE, float(duration)))
    if not overlay_is_audio:
        # 没有叠加音频时这些参数没有作用，冻结成明确的 0/关闭，而不是留着误导人。
        overlay_gain, start, loop = 0.0, 0.0, False
    else:
        loop = request['overlay_loop']
    return {'original_audio': request['original_audio'], 'original_gain': original_gain,
            'overlay_gain': overlay_gain, 'start_seconds': start,
            'overlay_loop': loop, 'fade_in_seconds': fade_in,
            'fade_out_seconds': fade_out, 'source_duration_seconds': float(duration),
            'source_has_audio': has_audio}


def prepare(task_id: str, source: Path, overlay: Path | None, snapshot_root: Path,
            parameters: dict[str, Any]) -> dict[str, Any]:
    if not task_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in task_id) \
            or len(task_id) > 128:
        raise ValueError('换音轨任务 ID 无效')
    if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError('换音轨的源文件必须是可用视频')
    if overlay is not None and (not overlay.is_file() or overlay.suffix.lower() not in AUDIO_SUFFIXES):
        raise ValueError('叠加音频不是可用音频文件')
    facts = creative_inspection.probe(source)
    frozen = validate(parameters, facts, overlay is not None)
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen_source = directory / ('source' + source.suffix.lower())
    shutil.copy2(source, frozen_source)
    request: dict[str, Any] = {'task_id': task_id, 'kind': 'audio_swap', **frozen,
                               'source': str(frozen_source.resolve())}
    if overlay is not None:
        frozen_overlay = directory / ('overlay' + overlay.suffix.lower())
        shutil.copy2(overlay, frozen_overlay)
        request['overlay'] = str(frozen_overlay.resolve())
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'audio_swap', 'request': request, 'request_path': str(request_path.resolve()),
            'output_dir': str((directory / 'result').resolve())}


def _wave_duration(path: Path) -> float:
    with wave.open(str(path), 'rb') as stream:
        frames, rate = stream.getnframes(), stream.getframerate()
    if frames <= 0 or rate <= 0:
        raise AudioSwapFailed(f'换音轨音频无效: {path.name}')
    return frames / rate


def build_command(request: dict[str, Any], output_video: Path) -> list[str]:
    duration = float(request['source_duration_seconds'])
    frame = f'{duration:.3f}'
    command = [str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y',
               '-i', request['source']]
    overlay = request.get('overlay')
    if overlay:
        if request['overlay_loop']:
            command += ['-stream_loop', '-1']
        command += ['-i', overlay]
    graph = []
    if request['original_audio'] == 'keep':
        graph.append(f"[0:a:0]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"volume={request['original_gain']}[base]")
    else:
        graph.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=0:{frame}[base]")
    labels = ['[base]']
    if overlay:
        delay = round(float(request['start_seconds']) * 1000)
        chain = (f"[1:a:0]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                 f"atrim=0:{frame},asetpts=PTS-STARTPTS")
        if request['fade_in_seconds']:
            chain += f",afade=t=in:st=0:d={request['fade_in_seconds']}"
        chain += f",adelay={delay}|{delay},volume={request['overlay_gain']}[bed]"
        graph.append(chain)
        labels.append('[bed]')
    mix = f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0"
    if request['fade_out_seconds']:
        fade_out = float(request['fade_out_seconds'])
        mix += f",afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out}"
    graph.append(f'{mix},alimiter=limit=0.95[aout]')
    command += ['-filter_complex', ';'.join(graph), '-map', '0:v:0', '-map', '[aout]',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', frame,
                '-movflags', '+faststart', str(output_video)]
    return command


def execute(payload: dict[str, Any], cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    request = payload['request']
    output_dir = Path(payload['output_dir'])
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / 'swapped.mp4'
    audio = output_dir / 'swapped-audio.wav'
    duration = float(request['source_duration_seconds'])
    if cancelled():
        raise AudioSwapStopped('换音轨任务在开始前停止')
    log_path = Path(payload['request_path']).with_name('execution.log')
    with log_path.open('wb') as log:
        try:
            subprocess.run(build_command(request, video), check=True, stdout=log, stderr=subprocess.STDOUT,
                           timeout=MAX_RUN_SECONDS, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y', '-i', str(video),
                            '-vn', '-ac', '2', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le', str(audio)],
                           check=True, stdout=log, stderr=subprocess.STDOUT, timeout=MAX_RUN_SECONDS,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioSwapFailed(f'换音轨失败: {exc}') from exc
    facts = creative_inspection.probe(video)
    if not facts.get('known') or facts.get('width') is None or not (facts.get('audio_streams') or 0):
        raise AudioSwapFailed('换音轨结果缺少画面或音轨')
    produced = facts.get('duration_seconds')
    if type(produced) not in (int, float) or not math.isfinite(produced) or abs(float(produced) - duration) > 0.25:
        raise AudioSwapFailed('换音轨结果时长与源视频不一致')
    audio_duration = _wave_duration(audio)
    if abs(audio_duration - duration) > 0.25:
        raise AudioSwapFailed('换音轨音轨时长与源视频不一致')
    receipt = {'task_id': request['task_id'], 'kind': 'audio_swap', 'status': 'succeeded',
               'output': str(video.resolve()), 'audio_output': str(audio.resolve()),
               'duration_seconds': float(produced), 'audio_duration_seconds': audio_duration,
               'original_audio': request['original_audio'], 'overlay': request.get('overlay'),
               'parameters': {key: request[key] for key in ('original_gain', 'overlay_gain', 'start_seconds',
                                                            'overlay_loop', 'fade_in_seconds', 'fade_out_seconds')}}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return receipt


def read_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Only accept this task's real output; a missing or foreign receipt stays unknown."""
    output_dir = Path(payload['output_dir'])
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('换音轨任务没有成功回执')
    if receipt.get('task_id') != payload['request']['task_id']:
        raise ValueError('换音轨回执与任务不一致')
    for key in ('output', 'audio_output'):
        path = Path(str(receipt.get(key) or ''))
        if not path.is_file() or path.parent != output_dir.resolve():
            raise ValueError('换音轨回执产物不在任务目录内')
    duration = float(receipt.get('duration_seconds', -1))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('换音轨回执时长无效')
    return receipt


def frozen_request(payload: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    request = payload.get('request')
    request_path = Path(str(payload.get('request_path') or ''))
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('换音轨冻结请求无效')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('换音轨请求快照与任务不一致')
    expected = {'task_id', 'kind', 'source', 'original_audio', 'original_gain', 'overlay_gain',
                'start_seconds', 'overlay_loop', 'fade_in_seconds', 'fade_out_seconds',
                'source_duration_seconds', 'source_has_audio'}
    if request.get('overlay'):
        expected.add('overlay')
    if set(request) != expected or request['kind'] != 'audio_swap':
        raise ValueError('换音轨冻结请求字段无效')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name:
        raise ValueError('换音轨任务目录不一致')
    if Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('换音轨输出目录不一致')
    for key in ('source', 'overlay'):
        value = request.get(key)
        if value is None:
            continue
        path = Path(str(value))
        if not path.is_absolute() or path.parent.resolve() != directory or not path.is_file():
            raise ValueError('换音轨输入不在冻结任务目录内')
    return request, directory / 'result'
