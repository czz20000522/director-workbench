"""Duration contract for the installed MiniMax H3 production recipes."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

FPS = 24
MIN_SECONDS = 4
MAX_SECONDS = 15


def plan(requested_seconds: Any, aspect_ratio: str = '1:1') -> dict[str, Any]:
    """Mirror this installation's ComfyMathExpression and H3 17k+5 grid."""
    if isinstance(requested_seconds, bool):
        raise ValueError('H3 请求时长必须是 4–15 秒的有限数字')
    try:
        seconds = float(requested_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError('H3 请求时长必须是 4–15 秒的有限数字') from exc
    if not math.isfinite(seconds) or not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ValueError('H3 请求时长必须在 4–15 秒之间，30 秒不受支持')
    if aspect_ratio not in {'1:1', '16:9', '9:16'}:
        raise ValueError('H3 画幅未登记')
    requested_frames = max(5, round(seconds * FPS))
    aligned_frames = requested_frames + (5 - requested_frames % 17) % 17
    dimensions = {'1:1': (640, 640), '16:9': (864, 480), '9:16': (480, 864)}
    width, height = dimensions[aspect_ratio]
    return {
        'requested_seconds': seconds, 'requested_frames': requested_frames,
        'frame_count': aligned_frames, 'playback_seconds': round(aligned_frames / FPS, 3),
        'fps': FPS, 'width': width, 'height': height,
        'aspect_ratio': aspect_ratio, 'megapixels': 0.4,
    }


def history_timing(entry: dict[str, Any]) -> dict[str, Any]:
    """Use ComfyUI's own enqueue/start/end millisecond timestamps, when present."""
    prompt = entry.get('prompt')
    queued_ms = prompt[3].get('create_time') if isinstance(prompt, (list, tuple)) and len(prompt) > 3 and isinstance(prompt[3], dict) else None
    messages = (entry.get('status') or {}).get('messages')
    stamps: dict[str, int] = {}
    if isinstance(messages, list):
        for message in messages:
            if (isinstance(message, (list, tuple)) and len(message) == 2
                    and message[0] in {'execution_start', 'execution_success', 'execution_error'}
                    and isinstance(message[1], dict)):
                stamp = message[1].get('timestamp')
                if type(stamp) is int and stamp > 0:
                    stamps[message[0]] = stamp
    started_ms = stamps.get('execution_start')
    ended_ms = stamps.get('execution_success') or stamps.get('execution_error')
    return {
        'source': 'comfyui_history_milliseconds',
        'queued_at_ms': queued_ms if type(queued_ms) is int and queued_ms > 0 else None,
        'execution_started_at_ms': started_ms,
        'execution_ended_at_ms': ended_ms,
        'queue_wait_seconds': round((started_ms - queued_ms) / 1000, 3)
        if type(queued_ms) is int and started_ms is not None and started_ms >= queued_ms else None,
        'comfy_execution_seconds': round((ended_ms - started_ms) / 1000, 3)
        if started_ms is not None and ended_ms is not None and ended_ms >= started_ms else None,
    }


def probe_video(path: Path) -> dict[str, Any] | None:
    """Read only the finished container metadata; unavailable data stays null."""
    try:
        import av
        with av.open(str(path)) as media:
            if not media.streams.video:
                return None
            video = media.streams.video[0]
            duration = (float(video.duration * video.time_base) if video.duration is not None and video.time_base is not None
                        else float(media.duration / av.time_base) if media.duration is not None else None)
            return {
                'duration_seconds': round(duration, 3) if duration is not None and math.isfinite(duration) and duration > 0 else None,
                'frames': video.frames if video.frames and video.frames > 0 else None,
                'width': video.width, 'height': video.height,
            }
    except Exception:
        # Optional diagnostics must never turn a successful GPU run into a
        # needs_reconcile task because a container probe failed.
        return None
