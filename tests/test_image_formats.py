"""HEIC/HEIF（iPhone 实况图主图）转 JPEG 的单元测试。

依赖 pillow-heif：没装的环境跳过，不让测试集整体变红。
"""
import io

import pytest

pytest.importorskip('pillow_heif')

from PIL import Image  # noqa: E402

from backend import image_formats  # noqa: E402


def _heic_bytes(size=(96, 64), color=(200, 120, 90)) -> bytes:
    pillow_heif = pytest.importorskip('pillow_heif')
    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='HEIF', quality=80)
    return buffer.getvalue()


def test_to_jpeg_returns_real_jpeg():
    payload = image_formats.to_jpeg(_heic_bytes())
    assert payload.startswith(b'\xff\xd8\xff')
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (96, 64)


def test_read_for_vision_converts_heic_and_reports_jpeg(tmp_path):
    target = tmp_path / 'IMG_0001.HEIC'
    target.write_bytes(_heic_bytes())
    payload, mime = image_formats.read_for_vision(target)
    assert mime == 'image/jpeg'
    assert payload.startswith(b'\xff\xd8\xff')


def test_read_for_vision_keeps_png_as_is(tmp_path):
    target = tmp_path / 'frame.png'
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (10, 20, 30)).save(buffer, format='PNG')
    original = buffer.getvalue()
    target.write_bytes(original)
    payload, mime = image_formats.read_for_vision(target)
    assert payload == original and mime == 'image/png'


def test_convert_upload_swaps_heic_for_jpg(tmp_path):
    source = tmp_path / 'live.heic'
    source.write_bytes(_heic_bytes())
    final = image_formats.convert_upload(source)
    assert final.name == 'live.jpg' and final.is_file()
    assert not source.exists()


def test_convert_upload_never_overwrites_existing_jpg(tmp_path):
    (tmp_path / 'live.jpg').write_bytes(b'old')
    source = tmp_path / 'live.heic'
    source.write_bytes(_heic_bytes())
    final = image_formats.convert_upload(source)
    assert final.name == 'live-1.jpg'
    assert (tmp_path / 'live.jpg').read_bytes() == b'old'


def test_convert_upload_ignores_other_formats(tmp_path):
    target = tmp_path / 'frame.png'
    target.write_bytes(b'\x89PNG\r\n\x1a\n')
    assert image_formats.convert_upload(target) == target
