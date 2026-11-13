"""Deterministic timeline export for the quick video workbench.

This module cuts, orders and joins existing clips with ffmpeg.  It deliberately
uses no model and no VRAM: trimming is exact, repeatable and finishes in
seconds, while a diffusion model would re-draw the footage instead of keeping
it.  AI edits (person removal, outpaint, continuation) belong to separate
experimental pipelines and must not be mixed into this path.

Probing goes through PyAV because the bundled ffmpeg build ships without
ffprobe; the actual export is a single ffmpeg invocation built from an explicit
filter graph, so nothing depends on shell quoting.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import av

from . import media_operations

VIDEO_SUFFIXES = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'}
MAX_CLIPS = 40
MAX_SECONDS = 1800.0
MIN_CLIP_SECONDS = 0.05
SAMPLE_RATE = 48_000


class VideoEditFailed(RuntimeError):
    pass


class VideoEditStopped(VideoEditFailed):
    pass


def capability() -> dict[str, Any]:
    engine = Path(media_operations.FFMPEG)
    return {
        'available': engine.is_file(),
        'engine': str(engine),
        'execution': 'local CPU',
        'operations': ['trim', 'order', 'concat', 'volume', 'mute', 'background_music'],
        'downloads': False,
    }


def _even(value: int) -> int:
    return max(2, int(value) - int(value) % 2)


def _rate(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if math.isfinite(rate) and 1 <= rate <= 240 else None


STANDARD_RATES = (23.976, 24, 25, 29.97, 30, 50, 59.94, 60)


def _snap_fps(rate: float) -> float:
    """把 23.66 这类均值帧率吸到最近的标称帧率。

    直接沿用平均值会一代一代往下漂（上一版 23.66 的输出再剪一次还是 23.66），
    拼接后播放器按 23.66 tbr 解释，动作会轻微不均匀。
    """
    best = min(STANDARD_RATES, key=lambda item: abs(item - rate))
    return best if abs(best - rate) <= 1.2 else round(rate, 3)


def probe(path: Path) -> dict[str, Any]:
    """Read duration, canvas, fps and audio presence without ffprobe."""
    if not path.is_file():
        raise VideoEditFailed(f'素材不存在：{path.name}')
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise VideoEditFailed(f'不是支持的视频格式：{path.name}')
    try:
        with av.open(str(path)) as container:
            video = next((stream for stream in container.streams if stream.type == 'video'), None)
            if video is None:
                raise VideoEditFailed(f'没有视频轨：{path.name}')
            duration = float(container.duration or 0) / 1_000_000
            if duration <= 0 and video.duration and video.time_base:
                duration = float(video.duration * video.time_base)
            if duration <= 0:
                raise VideoEditFailed(f'读不出时长：{path.name}')
            return {
                'duration': duration,
                'width': int(video.codec_context.width or 0),
                'height': int(video.codec_context.height or 0),
                'fps': _rate(video.average_rate) or _rate(video.guessed_rate) or 24.0,
                'has_audio': any(stream.type == 'audio' for stream in container.streams),
            }
    except VideoEditFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - PyAV raises a wide range of errors
        raise VideoEditFailed(f'无法读取「{path.name}」：{exc}') from exc


def _clip_window(clip: dict[str, Any], info: dict[str, Any], label: str) -> dict[str, Any]:
    start = float(clip.get('start_seconds') or 0)
    end = clip.get('end_seconds')
    limit = float(info['duration'])
    end = limit if end is None else min(float(end), limit)
    if start < 0 or end - start < MIN_CLIP_SECONDS:
        raise VideoEditFailed(f'「{label}」的入点/出点没有可用时长')
    return {**clip, 'label': label, 'start': start, 'end': end, 'length': end - start,
            'width': info['width'], 'height': info['height'], 'fps': info['fps'],
            'has_audio': info['has_audio']}


def _filter_graph(clips: list[dict[str, Any]], width: int, height: int, fps: float,
                  total: float, background: dict[str, Any] | None) -> str:
    parts: list[str] = []
    for index, clip in enumerate(clips):
        start, end = clip['start'], clip['end']
        parts.append(
            f'[{index}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,'
            f'scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
            f'fps={fps:.6f},format=yuv420p[v{index}]'
        )
        volume = 0.0 if clip.get('muted') else float(clip.get('volume') or 0)
        if clip['has_audio']:
            parts.append(
                f'[{index}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,'
                f'aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,'
                f'volume={volume:.3f}[a{index}]'
            )
        else:
            # concat 需要每段都有音轨；没有原声的片段补等长静音，导出才不会错位。
            parts.append(
                f'anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE},'
                f'atrim=duration={clip["length"]:.3f},asetpts=PTS-STARTPTS[a{index}]'
            )
    joined = ''.join(f'[v{index}][a{index}]' for index in range(len(clips)))
    parts.append(f'{joined}concat=n={len(clips)}:v=1:a=1[vcat][acat]')
    if background:
        parts.append(
            f'[{len(clips)}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,'
            f'aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,'
            f'volume={float(background["volume"]):.3f}[abgm]'
        )
        parts.append('[acat][abgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]')
    return ';'.join(parts)


def prepare(task_id: str, project_id: str, workspace: Path, clips: list[dict[str, Any]],
            background_music: dict[str, Any] | None = None, crf: int = 18) -> dict[str, Any]:
    """Freeze every input window and the exact ffmpeg command for one export."""
    if not clips:
        raise VideoEditFailed('至少要有一段素材')
    if len(clips) > MAX_CLIPS:
        raise VideoEditFailed(f'一次最多拼接 {MAX_CLIPS} 段')
    directory = workspace / 'video-edits' / task_id
    directory.mkdir(parents=True, exist_ok=False)
    prepared: list[dict[str, Any]] = []
    for index, clip in enumerate(clips, 1):
        path = Path(str(clip.get('path') or ''))
        info = probe(path)
        prepared.append(_clip_window(clip, info, str(clip.get('label') or f'第 {index} 段')))
    total = sum(clip['length'] for clip in prepared)
    if total > MAX_SECONDS:
        raise VideoEditFailed(f'成片 {total:.0f} 秒，超过 {MAX_SECONDS:.0f} 秒上限')
    width = _even(prepared[0]['width'])
    height = _even(prepared[0]['height'])
    fps = _snap_fps(prepared[0]['fps'])
    if width < 16 or height < 16:
        raise VideoEditFailed('第一段读不出有效画幅')
    background = None
    if background_music:
        music = Path(str(background_music.get('path') or ''))
        if not music.is_file():
            raise VideoEditFailed('背景音乐文件不存在')
        background = {'path': music, 'volume': float(background_music.get('volume') or 0.25)}
    output = directory / 'edit.mp4'
    command = [
        str(media_operations.FFMPEG), '-hide_banner', '-nostdin', '-y',
        *[part for clip in prepared for part in ('-i', str(clip['path']))],
        *(['-stream_loop', '-1', '-i', str(background['path'])] if background else []),
        '-filter_complex', _filter_graph(prepared, width, height, fps, total, background),
        '-map', '[vcat]', '-map', '[aout]' if background else '[acat]',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', str(int(crf)), '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-ar', str(SAMPLE_RATE), '-ac', '2',
        '-movflags', '+faststart', '-t', f'{total:.3f}', str(output),
    ]
    request = {
        'clips': [{'label': clip['label'], 'source': str(clip['path']),
                   'start_seconds': round(clip['start'], 3), 'end_seconds': round(clip['end'], 3),
                   'volume': clip.get('volume', 1), 'muted': bool(clip.get('muted'))}
                  for clip in prepared],
        'background_music': (str(background['path']) if background else None),
        'background_volume': (background['volume'] if background else None),
        'crf': int(crf),
    }
    payload = {
        'kind': 'video_edit', 'task_id': task_id, 'project_id': project_id,
        'command': command, 'output': str(output), 'total_seconds': total,
        'width': width, 'height': height, 'fps': fps, 'request': request,
    }
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def _tail(path: Path, limit: int = 1600) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return text[-limit:].strip()


def execute(payload: dict[str, Any], cancelled=lambda: False) -> dict[str, Any]:
    output = Path(payload['output'])
    output.parent.mkdir(parents=True, exist_ok=True)
    log = output.parent / 'ffmpeg.log'
    started = time.time()
    with log.open('w', encoding='utf-8', errors='replace') as handle:
        process = subprocess.Popen(  # noqa: S603 - argv built from validated paths
            payload['command'], stdout=subprocess.DEVNULL, stderr=handle,
            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        deadline = started + MAX_SECONDS * 4
        while process.poll() is None:
            if cancelled():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise VideoEditStopped('已停止剪辑导出')
            if time.time() > deadline:
                process.kill()
                raise VideoEditFailed('剪辑导出超时')
            time.sleep(0.2)
    if process.returncode != 0:
        raise VideoEditFailed(f'ffmpeg 导出失败：{_tail(log)}')
    if not output.is_file() or output.stat().st_size <= 0:
        raise VideoEditFailed('ffmpeg 没有产出文件')
    info = probe(output)
    return {
        'output': str(output),
        'outputs': [{'kind': 'video', 'path': str(output), 'title': '剪辑成片',
                     'duration_seconds': round(info['duration'], 3),
                     'width': info['width'], 'height': info['height']}],
        'duration_seconds': round(info['duration'], 3),
        'width': info['width'], 'height': info['height'],
        'size_bytes': output.stat().st_size,
        'elapsed_seconds': round(time.time() - started, 2),
    }
