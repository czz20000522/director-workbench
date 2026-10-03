import json
from pathlib import Path

import av
import numpy as np
import pytest

from backend.keyframes import prepare
from tools.qwen_image_edit import graph, reference_size


def reference_image(tmp_path):
    path = tmp_path / 'reference.png'
    with av.open(str(path), 'w', format='image2') as output:
        stream = output.add_stream('png')
        stream.width, stream.height, stream.pix_fmt = 32, 48, 'rgb24'
        frame = av.VideoFrame.from_ndarray(np.zeros((48, 32, 3), dtype=np.uint8), format='rgb24')
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


def test_recipe_freezes_reference_and_preserves_existing_attempt(tmp_path):
    reference = reference_image(tmp_path)
    original = reference.read_bytes()
    payload = prepare('attempt-1', '雨夜中挥手', reference, tmp_path / 'tasks', '系列工作目录/作品/keyframe-1', 42)
    recipe = json.loads(Path(payload['workflow']).read_text(encoding='utf-8'))
    assert Path(recipe['17']['inputs']['image']).read_bytes() == original
    assert recipe['13']['inputs']['prompt'] == '雨夜中挥手'
    assert recipe['15']['inputs']['seed'] == 42
    assert recipe['18']['inputs']['filename_prefix'] == '系列工作目录/作品/keyframe-1'
    assert payload['request']['reference_height'] == 48
    assert (recipe['16']['inputs']['width'], recipe['16']['inputs']['height']) == (832, 1248)
    assert payload['request']['width'] == 832
    with pytest.raises(FileExistsError):
        prepare('attempt-1', '另一画面', reference, tmp_path / 'tasks', 'other', 1)
    reference.write_bytes(b'changed after submission')
    assert Path(recipe['17']['inputs']['image']).read_bytes() == original
    assert json.loads(Path(payload['workflow']).read_text(encoding='utf-8')) == recipe


@pytest.mark.parametrize('prefix', ['../outside', '/absolute', 'C:/outside', 'a\\b', 'a/../b', 'a//b', ''])
def test_invalid_output_location_creates_no_snapshot(tmp_path, prefix):
    with pytest.raises(ValueError):
        prepare('attempt', '画面', reference_image(tmp_path), tmp_path / 'tasks', prefix, 1)
    assert not (tmp_path / 'tasks').exists()


def test_corrupt_reference_is_rejected_before_creating_snapshot(tmp_path):
    reference = tmp_path / 'fake.png'
    reference.write_bytes(b'not an image')
    with pytest.raises(ValueError, match='无法解码'):
        prepare('attempt', '画面', reference, tmp_path / 'tasks', 'valid/output', 1)
    assert not (tmp_path / 'tasks').exists()


@pytest.mark.parametrize('source,expected', [
    ((576, 1024), (720, 1280)), ((1920, 1080), (1280, 720)),
    ((1024, 1024), (1024, 1024)), ((768, 1344), (768, 1344)),
])
def test_edit_keeps_standard_camera_ratios_without_cropping(source, expected):
    recipe = graph(Path('not-required-to-exist.png'), 'edit', 'test', 1, source)
    scale = recipe['16']
    assert scale['class_type'] == 'ImageScale'
    assert scale['inputs']['crop'] == 'disabled'
    assert (scale['inputs']['width'], scale['inputs']['height']) == expected
    assert expected[0] * source[1] == expected[1] * source[0]
    # The sampler and both reference encoders see the same complete canvas.
    assert recipe['14']['inputs']['pixels'] == ['16', 0]
    assert recipe['9']['inputs']['image1'] == recipe['13']['inputs']['image1'] == ['16', 0]


@pytest.mark.parametrize('source', [(1001, 731), (777, 1355), (123, 456), (2047, 1081)])
def test_unusual_sizes_only_round_scaled_edges_to_vae_grid(source):
    width, height = reference_size(*source)
    factor = (1024 * 1024 / (source[0] * source[1])) ** 0.5
    assert width % 8 == height % 8 == 0
    assert abs(width - source[0] * factor) <= 4
    assert abs(height - source[1] * factor) <= 4


@pytest.mark.parametrize('dimensions', [(0, 100), (-8, 64), (True, 64), (512.5, 1024)])
def test_invalid_reference_dimensions_are_rejected(dimensions):
    with pytest.raises(ValueError):
        reference_size(*dimensions)


def test_cli_uses_decoded_reference_dimensions(tmp_path, monkeypatch):
    from tools import qwen_image_edit
    reference = reference_image(tmp_path)
    submitted = []

    def fake_request(base, path, payload=None):
        if path == '/prompt':
            submitted.append(payload['prompt'])
            return {'prompt_id': 'test'}
        return {'test': {'status': {'status_str': 'success'}, 'outputs': {}}}

    monkeypatch.setattr('sys.argv', ['qwen_image_edit', '--image', str(reference),
                                   '--prompt', 'edit', '--prefix', 'test'])
    monkeypatch.setattr(qwen_image_edit, 'request_json', fake_request)
    qwen_image_edit.main()
    assert submitted[0]['16']['inputs']['width'] == 832
    assert submitted[0]['16']['inputs']['height'] == 1248
