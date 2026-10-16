"""Bounded image/video super-resolution recipes; the workbench owns admission."""
import math
from pathlib import Path

import av
from pydantic import BaseModel, Field


MODEL = 'RealESRGAN_x2plus.pth'
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.mkv'}
MAX_VIDEO_PIXELS = 256_000_000


class EnhancementRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=160)


def inspect_source(path: Path) -> dict:
    """Decode inputs within a conservative whole-clip memory budget."""
    if path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        raise ValueError('支持 PNG、JPEG、WebP 图片及 MP4、MOV、WebM、MKV 视频')
    if path.stat().st_size > 2 * 1024 ** 3:
        raise ValueError('单个素材不能超过 2 GB')
    kind = 'image' if path.suffix.lower() in IMAGE_EXTENSIONS else 'video'
    with av.open(str(path)) as container:
        stream = next(iter(container.streams.video), None)
        if stream is None:
            raise ValueError('素材没有可读取的画面')
        if len(container.streams.audio) > 1:
            raise ValueError('本版只支持至多一条音轨的视频')
        width, height = stream.width, stream.height
        if width < 1 or height < 1 or width * height > (8_400_000 if kind == 'image' else 4_200_000):
            raise ValueError('输入图片最多 840 万像素，视频每帧最多 420 万像素')
        fps = float(stream.average_rate) if stream.average_rate else None
        duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
        if kind == 'video' and (not fps or not math.isfinite(fps) or not 1 <= fps <= 60):
            raise ValueError('视频需要可识别的 1–60 fps 帧率')
        count = 0
        for frame in container.decode(stream):
            count += 1
            if frame.width != width or frame.height != height:
                raise ValueError('暂不支持中途改变尺寸的视频')
            if kind == 'image' and count > 1:
                raise ValueError('请选择静态图片；动画请先转为视频')
            if kind == 'video' and (count > 600 or count / fps > 30 or count * width * height > MAX_VIDEO_PIXELS):
                raise ValueError('本版视频上限 30 秒、600 帧、累计 2.56 亿输入像素；请先按片段处理')
        if not count:
            raise ValueError('素材没有可解码的画面')
        return dict(kind=kind, width=width, height=height, frames=count,
                    fps=fps if kind == 'video' else None,
                    duration_seconds=duration if kind == 'video' else None,
                    audio_streams=len(container.streams.audio))


def image_graph(source: str, prefix: str) -> dict:
    return {
        'source': {'class_type': 'LoadImage', 'inputs': {'image': source}},
        'model': {'class_type': 'UpscaleModelLoader', 'inputs': {'model_name': MODEL}},
        'upscale': {'class_type': 'ImageUpscaleWithModel', 'inputs': {
            'image': ['source', 0], 'upscale_model': ['model', 0]}},
        'save': {'class_type': 'SaveImage', 'inputs': {
            'images': ['upscale', 0], 'filename_prefix': prefix}},
    }


def capability(info: dict) -> dict:
    common = ['UpscaleModelLoader', 'ImageUpscaleWithModel']
    model_input = info.get('UpscaleModelLoader', {}).get('input', {}).get('required', {}).get('model_name', [[]])
    models = (model_input[1].get('options', []) if model_input[0] == 'COMBO' and len(model_input) > 1
              else model_input[0])
    result = {'model': MODEL, 'scale': 2, 'kinds': {},
              'limits': '静态图片≤840万像素；视频≤30秒、600帧、累计2.56亿输入像素（1344×768约10秒）'}
    for kind, nodes in {'image': ['LoadImage', 'SaveImage'], 'video': [
            'LoadVideo', 'GetVideoComponents', 'CreateVideo', 'SaveVideo']}.items():
        missing = [node for node in common + nodes if node not in info]
        if MODEL not in models:
            missing.append(MODEL)
        result['kinds'][kind] = {'available': not missing, 'missing': missing}
    return result
