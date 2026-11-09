"""HEIC / HEIF（iPhone 实况图的主图）转 JPEG。

浏览器不显示 HEIC，多模态模型也只吃 png/jpeg。与其在每个环节都写分支，
不如**在上传落盘时**就换成 JPEG：预览、识图、参考图生成、素材库都照旧工作。

依赖 `pillow-heif`（随 venv 安装；便携版打包时要带上这个 wheel）。
它缺席时不让整个后端挂掉——只有碰到 HEIC 才报一句人话。
"""
from __future__ import annotations

import io
from pathlib import Path

HEIF_SUFFIXES = {'.heic', '.heif'}

_REGISTERED = False


class HeifUnavailable(RuntimeError):
    """本机没装 pillow-heif。"""


def _register() -> None:
    """把 HEIF 注册进 Pillow，全局一次即可。"""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        import pillow_heif
    except ImportError as exc:  # pragma: no cover - 只在缺依赖时走到
        raise HeifUnavailable('本机缺少 pillow-heif，读不了 HEIC/HEIF') from exc
    pillow_heif.register_heif_opener()
    _REGISTERED = True


def is_heif(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in HEIF_SUFFIXES


def to_jpeg(data: bytes, quality: int = 92) -> bytes:
    """HEIC/HEIF 字节 → JPEG 字节。按 EXIF 摆正方向，避免竖拍照片躺倒。"""
    _register()
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        upright = ImageOps.exif_transpose(image)
        if upright.mode not in {'RGB', 'L'}:
            upright = upright.convert('RGB')
        buffer = io.BytesIO()
        upright.save(buffer, format='JPEG', quality=quality, optimize=True)
    return buffer.getvalue()


def read_for_vision(path: Path) -> tuple[bytes, str]:
    """给模型看的字节与 MIME：HEIC 先转 JPEG，其它原样。"""
    payload = path.read_bytes()
    if is_heif(path):
        return to_jpeg(payload), 'image/jpeg'
    return payload, {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    }.get(path.suffix.lower(), 'image/png')


def convert_upload(destination: Path) -> Path:
    """上传落盘后调用：HEIC/HEIF 就地换成一个同名 JPEG，返回最终路径。

    同名 JPEG 已存在时加 -1 / -2（和上传接口的重名策略一致），绝不覆盖。
    """
    if destination.suffix.lower() not in HEIF_SUFFIXES:
        return destination
    jpeg = destination.with_suffix('.jpg')
    if jpeg.exists():
        stem = destination.stem
        for index in range(1, 1000):
            candidate = destination.with_name(f'{stem}-{index}.jpg')
            if not candidate.exists():
                jpeg = candidate
                break
    jpeg.write_bytes(to_jpeg(destination.read_bytes()))
    destination.unlink(missing_ok=True)
    return jpeg

# 导入即注册：素材库缩略图、清晰化等走 PIL 的路径也能直接读 HEIC。
try:
    _register()
except HeifUnavailable:  # pragma: no cover - 缺依赖时只影响 HEIC
    pass
