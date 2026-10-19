"""CPU-only, explainable continuity hints for adjacent assembly joins.

The values in this module are advisory measurements.  They are deliberately
kept separate from :mod:`backend.assembly_edit`, whose review state remains the
authority for assembly eligibility.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import media_operations


FRAME_SIZE = (32, 32)
AUDIO_WINDOW_SECONDS = 0.25
COMMAND_TIMEOUT_SECONDS = 20
SILENCE_RMS = 0.01
CLIPPING_PEAK = 0.98


def _tool_path(value: str | Path | None, name: str) -> Path | None:
    if value:
        candidate = Path(value)
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def _ffmpeg_path() -> Path | None:
    return _tool_path(getattr(media_operations, 'FFMPEG', None), 'ffmpeg')


def _ffprobe_path(ffmpeg: Path | None = None) -> Path | None:
    sibling = ffmpeg.with_name('ffprobe.exe') if ffmpeg else None
    if sibling and sibling.is_file():
        return sibling
    sibling = ffmpeg.with_name('ffprobe') if ffmpeg else None
    if sibling and sibling.is_file():
        return sibling
    return _tool_path(None, 'ffprobe')


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rate(value: Any) -> float | None:
    if isinstance(value, str) and '/' in value:
        numerator, denominator = value.split('/', 1)
        try:
            numerator_value = float(numerator)
            denominator_value = float(denominator)
            if denominator_value:
                return numerator_value / denominator_value
        except ValueError:
            return None
    return _number(value)


def _rotation(stream: dict[str, Any]) -> float:
    tags = stream.get('tags') or {}
    value = _number(tags.get('rotate'))
    if value is not None:
        return value % 360
    for side_data in stream.get('side_data_list') or []:
        value = _number(side_data.get('rotation'))
        if value is not None:
            return value % 360
    return 0.0


def _probe_media(path: Path, ffprobe: Path) -> dict[str, Any]:
    result = subprocess.run(
        [str(ffprobe), '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', str(path)],
        capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or 'ffprobe failed').strip()[:500])
    payload = json.loads(result.stdout or '{}')
    streams = payload.get('streams') or []
    video = next((item for item in streams if item.get('codec_type') == 'video'), None)
    audio = next((item for item in streams if item.get('codec_type') == 'audio'), None)
    fmt = payload.get('format') or {}
    if video is None:
        raise RuntimeError('未找到视频轨')
    duration = _number(video.get('duration')) or _number(fmt.get('duration'))
    return {
        'video': {
            'width': video.get('width'),
            'height': video.get('height'),
            'fps': _rate(video.get('avg_frame_rate')) or _rate(video.get('r_frame_rate')),
            'rotation': _rotation(video),
            'aspect_ratio': round(float(video['width']) / float(video['height']), 6)
            if video.get('width') and video.get('height') else None,
            'duration_seconds': round(duration, 6) if duration is not None else None,
        },
        'audio': {
            'sample_rate': int(audio['sample_rate']) if audio and str(audio.get('sample_rate', '')).isdigit() else None,
            'channels': int(audio['channels']) if audio and str(audio.get('channels', '')).isdigit() else None,
            'present': audio is not None,
        },
    }


def _extract_raw_frame(ffmpeg: Path, source: Path, output: Path, *, last: bool) -> None:
    position = ['-sseof', '-0.05'] if last else ['-ss', '0']
    result = subprocess.run(
        [str(ffmpeg), '-nostdin', '-v', 'error', *position, '-i', str(source),
         '-frames:v', '1', '-vf', 'scale=32:32:force_original_aspect_ratio=decrease,'
         'pad=32:32:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24',
         '-f', 'rawvideo', '-y', str(output)],
        capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError((result.stderr or '无法抽取边界画面').strip()[:500])


def _frame_metrics(left: Path, right: Path) -> dict[str, Any]:
    left_data = left.read_bytes()
    right_data = right.read_bytes()
    size = min(len(left_data), len(right_data))
    if size < 3:
        raise RuntimeError('边界画面为空')
    left_data = left_data[:size - (size % 3)]
    right_data = right_data[:size - (size % 3)]
    pixels = len(left_data) // 3
    left_mean = tuple(round(sum(left_data[index::3]) / pixels, 3) for index in range(3))
    right_mean = tuple(round(sum(right_data[index::3]) / pixels, 3) for index in range(3))
    brightness_left = round(sum((0.2126 * left_data[i] + 0.7152 * left_data[i + 1] + 0.0722 * left_data[i + 2])
                                for i in range(0, len(left_data), 3)) / pixels, 3)
    brightness_right = round(sum((0.2126 * right_data[i] + 0.7152 * right_data[i + 1] + 0.0722 * right_data[i + 2])
                                 for i in range(0, len(right_data), 3)) / pixels, 3)
    difference = sum(abs(a - b) for a, b in zip(left_data, right_data)) / (len(left_data) * 255)
    return {
        'available': True,
        'method': '32x32 RGB boundary frame heuristic',
        'left_average_rgb': left_mean,
        'right_average_rgb': right_mean,
        'left_brightness': brightness_left,
        'right_brightness': brightness_right,
        'brightness_delta': round(abs(brightness_left - brightness_right), 3),
        'mean_absolute_rgb_difference': round(difference, 4),
        'note': '启发式、非艺术判断；仅供人工审核参考',
    }


def _extract_audio_window(ffmpeg: Path, source: Path, output: Path, *, last: bool) -> None:
    position = ['-sseof', f'-{AUDIO_WINDOW_SECONDS}'] if last else ['-ss', '0']
    duration = ['-t', str(AUDIO_WINDOW_SECONDS)] if not last else []
    result = subprocess.run(
        [str(ffmpeg), '-nostdin', '-v', 'error', *position, '-i', str(source), '-vn',
         *duration, '-ac', '1', '-ar', '8000', '-f', 'f32le', '-y', str(output)],
        capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError((result.stderr or '无法测量边界音频').strip()[:500])


def _audio_metrics(path: Path) -> dict[str, Any]:
    import array

    values = array.array('f')
    values.frombytes(path.read_bytes())
    if not values:
        raise RuntimeError('边界音频为空')
    rms = math.sqrt(sum(float(value) ** 2 for value in values) / len(values))
    peak = max(abs(float(value)) for value in values)
    return {'rms': round(rms, 5), 'peak': round(peak, 5), 'silent': rms < SILENCE_RMS,
            'clipping': peak >= CLIPPING_PEAK}


def _compare_specs(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    video_left, video_right = left['video'], right['video']
    video = {'left': video_left, 'right': video_right, 'differences': {}}
    for key, label in (('width', '分辨率宽度'), ('height', '分辨率高度'), ('fps', '帧率'),
                       ('rotation', '旋转'), ('aspect_ratio', '宽高比'), ('duration_seconds', '时长')):
        a, b = video_left.get(key), video_right.get(key)
        different = a is not None and b is not None and abs(float(a) - float(b)) > (0.01 if key in {'fps', 'aspect_ratio', 'duration_seconds'} else 0)
        video['differences'][key] = {'different': different, 'delta': round(abs(float(a) - float(b)), 6) if different else 0}
        if different:
            warnings.append(f'{label}不同')
    audio_left, audio_right = left['audio'], right['audio']
    audio = {'left': audio_left, 'right': audio_right, 'differences': {}}
    for key, label in (('sample_rate', '采样率'), ('channels', '声道数')):
        a, b = audio_left.get(key), audio_right.get(key)
        different = a is not None and b is not None and a != b
        audio['differences'][key] = {'different': different}
        if different:
            warnings.append(f'{label}不同')
    if audio_left.get('present') != audio_right.get('present'):
        warnings.append('仅一侧存在音频轨')
    return {'video': video, 'audio': audio}, warnings


def analyze_join(left: dict[str, Any], right: dict[str, Any], *, resolve_path: Callable[[str], Path] | None = None,
                 ffmpeg: str | Path | None = None, ffprobe: str | Path | None = None,
                 temp_root: str | Path | None = None) -> dict[str, Any]:
    """Return one stable, JSON-serializable advisory row without raising media errors."""
    ffmpeg_path = _tool_path(ffmpeg, 'ffmpeg') if ffmpeg is not None else _ffmpeg_path()
    ffprobe_path = _tool_path(ffprobe, 'ffprobe') if ffprobe is not None else _ffprobe_path(ffmpeg_path)
    base = {'advisory': True, 'available': False, 'status': 'unavailable', 'warnings': [],
            'video': {}, 'frames': {}, 'audio': {},
            'note': '启发式、非艺术判断；不会自动通过或阻止装配'}
    if not ffmpeg_path or not ffprobe_path:
        base['reason'] = 'FFmpeg/ffprobe 不可用，未执行连续性提示'
        return base

    paths: list[Path] = []
    for side in (left, right):
        value = (side.get('video') or {}).get('path')
        if not value:
            base['reason'] = '接点一侧没有登记视频'
            return base
        try:
            path = resolve_path(str(value)) if resolve_path else Path(str(value)).resolve()
            if not path.is_file():
                raise FileNotFoundError(str(path))
            paths.append(path)
        except (OSError, ValueError) as exc:
            base['reason'] = f'视频不可用：{exc}'
            return base

    try:
        specs = [_probe_media(path, ffprobe_path) for path in paths]
        comparison, warnings = _compare_specs(*specs)
        base.update(available=True, status='ok', video=comparison['video'], audio=comparison['audio'], warnings=warnings)
        with tempfile.TemporaryDirectory(prefix='assembly-continuity-', dir=str(temp_root) if temp_root else None) as folder:
            root = Path(folder)
            left_frame, right_frame = root / 'left.rgb', root / 'right.rgb'
            _extract_raw_frame(ffmpeg_path, paths[0], left_frame, last=True)
            _extract_raw_frame(ffmpeg_path, paths[1], right_frame, last=False)
            base['frames'] = _frame_metrics(left_frame, right_frame)
            left_audio, right_audio = root / 'left.f32', root / 'right.f32'
            if specs[0]['audio']['present'] and specs[1]['audio']['present']:
                _extract_audio_window(ffmpeg_path, paths[0], left_audio, last=True)
                _extract_audio_window(ffmpeg_path, paths[1], right_audio, last=False)
                measurements = {'left': _audio_metrics(left_audio), 'right': _audio_metrics(right_audio)}
                base['audio']['boundary'] = measurements
                if measurements['left']['silent'] or measurements['right']['silent']:
                    base['warnings'].append('接点附近可能存在静音')
                if measurements['left']['clipping'] or measurements['right']['clipping']:
                    base['warnings'].append('接点附近可能存在爆音/削波')
            else:
                base['audio']['boundary'] = {'available': False, 'reason': '两侧均需有音频轨才能测量边界'}
        return base
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        base['reason'] = str(exc)[:500] or '媒体测量失败'
        return base


def analyze_joins(segments: list[dict[str, Any]], *, resolve_path: Callable[[str], Path] | None = None,
                  ffmpeg: str | Path | None = None, ffprobe: str | Path | None = None,
                  temp_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Analyze all adjacent segment pairs while preserving their stable join keys."""
    rows = []
    clean = [item for item in segments if isinstance(item, dict)]
    for left, right in zip(clean, clean[1:]):
        row = analyze_join(left, right, resolve_path=resolve_path, ffmpeg=ffmpeg,
                           ffprobe=ffprobe, temp_root=temp_root)
        row.update(key=f"{left.get('id')}>{right.get('id')}", left_id=left.get('id'), right_id=right.get('id'))
        rows.append(row)
    return rows


def analyze_document(document: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    rows = analyze_joins(document.get('segments') or [], **kwargs)
    return {
        'advisory': True,
        'available': all(row.get('available') for row in rows) if rows else True,
        'method': 'CPU FFmpeg/ffprobe heuristics',
        'note': '启发式、非艺术判断；人工审核仍是装配的权威，不自动通过或阻止装配',
        'joins': rows,
    }
