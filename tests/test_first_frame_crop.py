"""图生视频送进引擎前按方案比例裁切首帧。

H3 的图生视频不是裁剪而是**拉伸**：1344×768 的首帧配 9:16 方案，出片是 608×1056，
人脸会被横向压扁。这里锁住「先裁后送」，避免以后又退回去。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend import app as backend


def _make_image(path: Path, size: tuple[int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, (120, 140, 160)).save(path)
    return path


def test_first_frame_is_cropped_to_the_preset_ratio(preset_client, tmp_path, monkeypatch):
    _, project = preset_client
    monkeypatch.setattr(backend, 'COMFY_INPUT_ROOT', tmp_path / 'comfy-input')
    frame = _make_image(project / 'workspaces' / 'preset-test' / 'frame-wide.png', (1344, 768))
    graph = {
        '114': {'class_type': 'LoadImage', 'inputs': {'image': str(frame)}},
        '115': {'class_type': 'ResolutionSelector',
                'inputs': {'aspect_ratio': '9:16 (Portrait Widescreen)', 'megapixels': 0.6}},
    }

    staged = backend.stage_comfy_input_files(graph, 'preset-test')

    with Image.open(staged[0]['path']) as result:
        assert abs(result.width / result.height - 9 / 16) < 0.01
        # 裁切不放大：高度仍是 768，宽度收到 432。
        assert (result.width, result.height) == (432, 768)
    assert not graph['114']['inputs']['image'].endswith('.png\\')


def test_a_matching_frame_is_copied_untouched(preset_client, tmp_path, monkeypatch):
    _, project = preset_client
    monkeypatch.setattr(backend, 'COMFY_INPUT_ROOT', tmp_path / 'comfy-input')
    frame = _make_image(project / 'workspaces' / 'preset-test' / 'frame-tall.png', (576, 1024))
    graph = {
        '114': {'class_type': 'LoadImage', 'inputs': {'image': str(frame)}},
        '115': {'class_type': 'ResolutionSelector', 'inputs': {'aspect_ratio': '9:16 (Portrait)'}},
    }

    staged = backend.stage_comfy_input_files(graph, 'preset-test')

    with Image.open(staged[0]['path']) as result:
        assert (result.width, result.height) == (576, 1024)


def test_images_without_a_video_preset_are_not_cropped(preset_client, tmp_path, monkeypatch):
    """参考图/清晰化也走 LoadImage，没有 ResolutionSelector 时不能动人家的构图。"""
    _, project = preset_client
    monkeypatch.setattr(backend, 'COMFY_INPUT_ROOT', tmp_path / 'comfy-input')
    frame = _make_image(project / 'workspaces' / 'preset-test' / 'reference.png', (1344, 768))
    graph = {'1': {'class_type': 'LoadImage', 'inputs': {'image': str(frame)}}}

    staged = backend.stage_comfy_input_files(graph, 'preset-test')

    with Image.open(staged[0]['path']) as result:
        assert (result.width, result.height) == (1344, 768)
