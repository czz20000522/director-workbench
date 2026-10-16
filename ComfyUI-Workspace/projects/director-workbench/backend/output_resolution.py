"""Bounded streaming output resizing; native model output is always retained."""
from pathlib import Path
import os
import subprocess
import threading
import time
import uuid

from .media_operations import FFMPEG
from .h3_duration import probe_video

_LOCK = threading.Lock()


class OutputStopped(RuntimeError):
    pass


def dimensions(resolution, aspect):
    if resolution not in {'native', '2k', '4k', '8k'} or aspect not in {'16:9', '9:16', '1:1'}:
        raise ValueError('输出画幅或分辨率无效')
    if resolution == 'native':
        return None
    long_edge = {'2k': 2048, '4k': 3840, '8k': 7680}[resolution]
    short = long_edge * 9 // 16
    return (long_edge, short) if aspect == '16:9' else (short, long_edge) if aspect == '9:16' else (long_edge, long_edge)


def require_available(resolution):
    if resolution != 'native' and not FFMPEG.is_file():
        raise ValueError('当前服务缺少输出尺寸处理工具，请选择原生分辨率或联系管理员')


def resize(source, root, resolution, aspect, stopped=lambda: False):
    size = dimensions(resolution, aspect)
    if size is None:
        return Path(source), None
    require_available(resolution)
    source, root = Path(source).resolve(), Path(root).resolve()
    if not source.is_relative_to(root) or not source.is_file() or source.suffix.lower() not in {'.png', '.mp4'}:
        raise ValueError('尺寸处理仅接受本任务输出目录中的图片或视频')
    target = source.with_name(f'{source.stem}-output-{resolution}{source.suffix}')
    if target.resolve().parent != source.parent:
        raise ValueError('输出尺寸目标越界')
    width, height = size
    def valid(path):
        metadata = probe_video(path)
        return metadata and (metadata['width'], metadata['height']) == size
    # Reconciliation uses the same target, and never starts a second model run.
    with _LOCK:
        if stopped():
            raise OutputStopped('输出尺寸处理已停止，原始生成结果保留')
        if not (target.is_file() and valid(target)):
            temporary = source.with_name(f'{source.stem}-resize-{uuid.uuid4().hex}{source.suffix}')
            log = temporary.with_suffix(temporary.suffix + '.log')
            command = [str(FFMPEG), '-nostdin', '-n', '-hide_banner', '-loglevel', 'error',
                       '-threads', '2', '-i', str(source), '-map', '0:v:0', '-vf',
                       f'scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1',
                       '-filter_threads', '1']
            if source.suffix.lower() == '.png':
                command += ['-frames:v', '1', '-threads', '1']
            else:
                command += ['-map', '0:a?', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                            '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart']
            command.append(str(temporary))
            with log.open('wb') as output:
                process = subprocess.Popen(command, stdout=output, stderr=output,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                start = time.monotonic()
                try:
                    while process.poll() is None:
                        if stopped():
                            raise OutputStopped('输出尺寸处理已停止，原始生成结果保留')
                        if time.monotonic() - start > 1800:
                            raise RuntimeError('输出尺寸处理超时，原始结果保留，可核对任务后重试')
                        time.sleep(.2)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            if process.returncode or not valid(temporary):
                raise RuntimeError('输出尺寸处理失败，原始结果保留；请检查尺寸处理日志后核对任务')
            os.replace(temporary, target)
        if stopped():
            raise OutputStopped('输出尺寸处理已停止，原始生成结果保留')
    return target, {'resolution': resolution, 'width': width, 'height': height,
                    'method': 'lanczos_resize', 'native_output': str(source),
                    'note': '尺寸放大并居中微裁切，不代表原生高分辨率或 AI 细节超分'}
