from pathlib import Path

import pytest

from backend import image_superres, output_resolution
from support import reference_image


def test_passes_only_when_needed_and_never_beyond_4x():
    assert image_superres.passes_for(1536, 2048) == 1
    assert image_superres.passes_for(1536, 3840) == 2
    # 8K would need three passes (a 150 MP intermediate) for no visible gain.
    assert image_superres.passes_for(1536, 7680) == 2
    assert image_superres.passes_for(2304, 2048) == 0
    assert image_superres.passes_for(0, 2048) == 0


def test_graph_is_one_load_one_upscale_one_save():
    graph = image_superres._graph('staged.png', 'superres/0')
    assert graph['load']['inputs']['image'] == 'staged.png'
    assert graph['model']['inputs']['model_name'] == image_superres.MODEL
    assert graph['upscale']['inputs'] == {'upscale_model': ['model', 0], 'image': ['load', 0]}
    assert graph['save']['inputs']['images'] == ['upscale', 0]


def test_missing_model_keeps_the_native_render(tmp_path):
    source = reference_image(tmp_path)
    result = image_superres.upscale(source, (2048, 1152), input_root=tmp_path / 'in',
                                    output_root=tmp_path / 'out', model_root=tmp_path / 'models')
    assert result is None


@pytest.mark.host_tool
def test_resize_uses_the_sharper_base_and_cleans_staging(tmp_path):
    source = reference_image(tmp_path)
    staging = tmp_path / 'staged.png'
    staging.write_bytes(source.read_bytes())
    seen = {}

    def enhance(src, size, stopped):
        seen['source'], seen['size'] = src, size
        return staging, {'method': 'ai_upscale', 'model': image_superres.MODEL, 'passes': 1,
                         'scale': 2, 'superseded_size': '32 × 48', 'upscaled_size': '64 × 96',
                         'temporary': [str(staging)]}

    target, profile = output_resolution.resize(source, tmp_path, '2k', '16:9', enhance=enhance)
    assert seen == {'source': source, 'size': (2048, 1152)}
    assert profile['method'] == 'ai_upscale'
    assert profile['ai_upscale']['passes'] == 1
    assert 'temporary' not in profile['ai_upscale'], 'staging paths are not part of the receipt'
    assert profile['native_output'] == str(source)
    assert not staging.exists(), 'the staging copy is deleted once the export exists'
    assert target.is_file() and target != source


@pytest.mark.host_tool
def test_resize_falls_back_when_the_detail_pass_declines(tmp_path):
    source = reference_image(tmp_path)
    target, profile = output_resolution.resize(source, tmp_path, '2k', '16:9',
                                               enhance=lambda src, size, stopped: None)
    assert profile['method'] == 'lanczos_resize'
    assert not (tmp_path / 'staged.png').exists()
    assert target.is_file()
