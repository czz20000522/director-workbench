"""Explicit H3 multi-reference first-frame recipe contract tests.

These tests exercise the pure graph builder and environment validation. The
only optional live check is marked ``host_tool`` and validates against the
running local ComfyUI; no GPU generation is started by this test module.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import multiref_recipe as recipe
from backend.multiref_recipe import (
    AUDIO_VAE,
    CLIP_MODEL,
    MAX_PICTURES,
    MAX_REF_IMAGES,
    STILL_UNET,
    VIDEO_UNET,
    VIDEO_VAE,
    MultiRefError,
    MultiRefRequest,
    build_graph,
    environment_facts,
    prepare,
    validate_graph,
)


def request(**overrides) -> MultiRefRequest:
    payload = {
        'character_ref_images': ['char-a.png', 'char-b.png'],
        'scene_image': 'scene.png',
        'prompt': '角色站在窗边，镜头缓慢推近',
        'duration_seconds': 5,
        'aspect_ratio': '16:9',
        'seed': 42,
    }
    payload.update(overrides)
    return MultiRefRequest(**payload)


def _fake_object_info() -> dict:
    outputs = {
        'LoadImage': ['IMAGE', 'MASK'],
        'UNETLoader': ['MODEL'],
        'CLIPLoader': ['CLIP'],
        'VAELoader': ['VAE'],
        'MiniMaxH3StillConditioningT8': ['CONDITIONING', 'LATENT', 'STRING', 'STRING', 'STRING'],
        'MiniMaxH3DualClockSamplerT8': ['MODEL', 'SAMPLER', 'SIGMAS'],
        'MiniMaxH3StillDecodeT8': ['IMAGE', 'IMAGE', 'STRING'],
        'MiniMaxH3ImageToVideo': ['CONDITIONING', 'LATENT'],
        'MiniMaxH3TimedImageReferenceT8Advanced': ['CLIP'],
        'BasicGuider': ['GUIDER'],
        'RandomNoise': ['NOISE'],
        'SamplerCustomAdvanced': ['LATENT', 'LATENT'],
        'VAEDecode': ['IMAGE'],
        'VAEDecodeAudio': ['AUDIO'],
        'CreateVideo': ['VIDEO'],
        'SaveVideo': ['VIDEO'],
    }
    info = {
        name: {'input': {'required': {}}, 'output': output, 'api_node': False}
        for name, output in outputs.items()
    }
    info['LoadImage']['input']['required']['image'] = [
        ['example.png', 'scene.png', 'first.png', 'char-a.png', 'char-b.png'], {}
    ]
    info['UNETLoader']['input']['required']['unet_name'] = [[STILL_UNET, VIDEO_UNET], {}]
    info['CLIPLoader']['input']['required']['clip_name'] = [[CLIP_MODEL], {}]
    info['VAELoader']['input']['required']['vae_name'] = [[VIDEO_VAE, AUDIO_VAE], {}]
    return info


def test_build_graph_freezes_reference_order_and_connects_still_to_i2v():
    graph = build_graph(request())
    assert graph['load_primary']['inputs']['image'] == 'scene.png'
    assert graph['load_ref_0']['inputs']['image'] == 'char-a.png'
    assert graph['load_ref_1']['inputs']['image'] == 'char-b.png'
    still_inputs = graph['still']['inputs']
    assert still_inputs['edit_image'] == ['load_primary', 0]
    assert still_inputs['ref_images.ref_image_0'] == ['load_ref_0', 0]
    assert still_inputs['ref_images.ref_image_1'] == ['load_ref_1', 0]
    assert still_inputs['canvas_mode'] == 'custom'
    assert still_inputs['width'] == 1344 and still_inputs['height'] == 768
    assert still_inputs['target_mode'] == 'direct_1_frame'
    assert graph['still']['class_type'] == 'MiniMaxH3StillConditioningT8'
    assert graph['still_unet']['inputs']['unet_name'] == STILL_UNET
    assert graph['video_unet']['inputs']['unet_name'] == VIDEO_UNET
    assert graph['clip']['inputs']['clip_name'] == CLIP_MODEL
    assert graph['vae_video']['inputs']['vae_name'] == VIDEO_VAE
    assert graph['vae_audio']['inputs']['vae_name'] == AUDIO_VAE
    assert graph['i2v']['inputs']['first_frame'] == ['still_decode', 0]
    assert 'last_frame' not in graph['i2v']['inputs']
    assert graph['i2v']['inputs']['prompt'] == graph['still']['inputs']['prompt']
    assert graph['i2v']['inputs']['length'] == 124
    assert graph['video_sample']['inputs']['latent_image'] == ['i2v', 1]
    assert graph['create_video']['inputs']['images'] == ['video_decode', 0]
    assert graph['create_video']['inputs']['audio'] == ['audio_decode', 0]
    assert graph['create_video']['inputs']['fps'] == 24.0
    assert graph['save_video']['inputs']['video'] == ['create_video', 0]
    assert all(not str(node['class_type']).startswith('ComfyCloud') for node in graph.values())


def test_without_scene_first_character_is_picture_one():
    graph = build_graph(request(scene_image=None, character_ref_images=['a.png', 'b.png', 'c.png']))
    assert graph['load_primary']['inputs']['image'] == 'a.png'
    assert [graph['load_ref_0']['inputs']['image'], graph['load_ref_1']['inputs']['image']] == ['b.png', 'c.png']
    assert 'ref_images.ref_image_2' not in graph['still']['inputs']


def test_single_reference_is_explicit_and_uses_no_ref_slot():
    graph = build_graph(request(scene_image=None, character_ref_images=['only.png']))
    assert graph['load_primary']['inputs']['image'] == 'only.png'
    assert not [key for key in graph['still']['inputs'] if key.startswith('ref_images.')]


def test_first_frame_mode_skips_still_stage_and_chains_timed_references():
    graph = build_graph(request(
        first_frame_image='first.png', scene_image=None,
        character_ref_images=['char-a.png', 'char-b.png'],
    ))
    assert 'still' not in graph
    assert 'still_unet' not in graph
    assert 'still_decode' not in graph
    assert graph['load_first_frame']['inputs']['image'] == 'first.png'
    assert graph['i2v']['inputs']['first_frame'] == ['load_first_frame', 0]
    assert graph['timed_ref_0']['class_type'] == 'MiniMaxH3TimedImageReferenceT8Advanced'
    assert graph['timed_ref_0']['inputs']['clip'] == ['clip', 0]
    assert graph['timed_ref_0']['inputs']['prompt_tag'] == 'dshRef1'
    assert graph['timed_ref_1']['inputs']['clip'] == ['timed_ref_0', 0]
    assert graph['timed_ref_1']['inputs']['prompt_tag'] == 'dshRef2'
    assert graph['i2v']['inputs']['clip'] == ['timed_ref_1', 0]
    assert graph['i2v']['inputs']['prompt'].endswith('#dshRef1 #dshRef2')
    assert graph['video_unet']['inputs']['unet_name'] == VIDEO_UNET


def test_first_frame_without_references_is_allowed():
    graph = build_graph(request(first_frame_image='first.png', scene_image=None,
                                character_ref_images=[]))
    assert graph['i2v']['inputs']['first_frame'] == ['load_first_frame', 0]
    assert graph['i2v']['inputs']['clip'] == ['clip', 0]
    assert not [node_id for node_id in graph if node_id.startswith('timed_ref_')]
    assert not [node_id for node_id in graph if node_id.startswith('still')]


def test_first_frame_and_scene_are_mutually_exclusive():
    with pytest.raises(MultiRefError) as caught:
        build_graph(request(first_frame_image='first.png', scene_image='scene.png'))
    assert caught.value.code == 'first_frame_scene_exclusive'
    assert caught.value.status_code == 422


def test_no_reference_images_is_a_structured_blocker():
    with pytest.raises(MultiRefError) as caught:
        build_graph(request(character_ref_images=[]))
    error = caught.value
    assert error.code == 'reference_images_required'
    assert error.status_code == 422
    assert error.detail['blockers'][0]['code'] == 'reference_images_required'


def test_reference_limit_is_enforced_with_and_without_scene():
    # 9 characters without a scene = Picture 1 + 8 ref_images (valid boundary).
    build_graph(request(scene_image=None, character_ref_images=[f'c{i}.png' for i in range(9)]))
    # 10 characters exceed edit_image + 8 ref_images.
    with pytest.raises(MultiRefError) as caught:
        build_graph(request(scene_image=None, character_ref_images=[f'c{i}.png' for i in range(10)]))
    assert caught.value.code == 'too_many_reference_images'
    # A scene plus 8 characters is the other valid boundary; 9 characters is too many.
    build_graph(request(character_ref_images=[f'c{i}.png' for i in range(8)]))
    with pytest.raises(MultiRefError) as caught:
        build_graph(request(character_ref_images=[f'c{i}.png' for i in range(9)]))
    assert caught.value.code == 'too_many_reference_images'
    assert MAX_REF_IMAGES == 8 and MAX_PICTURES == 9


@pytest.mark.parametrize(('overrides', 'code'), [
    ({'prompt': '   '}, 'prompt_required'),
    ({'duration_seconds': 3.9}, 'invalid_duration'),
    ({'duration_seconds': 15.1}, 'invalid_duration'),
    ({'aspect_ratio': '4:3'}, 'invalid_aspect_ratio'),
    ({'character_ref_images': ['char/evil.png']}, 'invalid_reference_image'),
    ({'scene_image': '../scene.png'}, 'invalid_scene_image'),
    ({'output_prefix': '../escape'}, 'invalid_output_prefix'),
])
def test_invalid_inputs_return_structured_blockers(overrides, code):
    with pytest.raises(MultiRefError) as caught:
        build_graph(request(**overrides))
    assert caught.value.code == code
    assert caught.value.detail['blockers']


def test_prepare_validates_graph_against_object_info():
    body = prepare(request(), object_info=_fake_object_info())
    assert body['schema_version'] == 1
    assert body['recipe'] == recipe.RECIPE_ID
    assert body['environment']['ready'] is True
    assert body['metadata']['primary_role'] == 'scene'
    assert body['metadata']['frames'] == 124
    assert body['metadata']['actual_duration_seconds'] == 5.167
    assert body['metadata']['cloud_api'] is False
    assert body['prompt']['i2v']['inputs']['first_frame'] == ['still_decode', 0]


def test_prepare_validates_first_frame_graph_against_object_info():
    body = prepare(request(first_frame_image='first.png', scene_image=None,
                           character_ref_images=['char-a.png']),
                   object_info=_fake_object_info())
    assert body['environment']['ready'] is True
    assert body['metadata']['mode'] == 'first_frame_reference'
    assert body['metadata']['reference_prompt_tags'] == ['dshRef1']
    assert '#dshRef1' in body['metadata']['video_prompt']
    assert body['prompt']['i2v']['inputs']['first_frame'] == ['load_first_frame', 0]


def test_environment_reports_missing_node_and_model():
    info = _fake_object_info()
    del info['MiniMaxH3StillConditioningT8']
    facts = environment_facts(info)
    assert facts['ready'] is False
    assert 'node_missing' in {item['code'] for item in facts['blockers']}
    with pytest.raises(MultiRefError) as caught:
        prepare(request(), object_info=info)
    assert caught.value.code == 'node_missing'

    info = _fake_object_info()
    info['UNETLoader']['input']['required']['unet_name'] = [[VIDEO_UNET], {}]
    facts = environment_facts(info)
    assert facts['ready'] is False
    assert any(item['code'] == 'model_missing' and item['model'] == STILL_UNET
               for item in facts['blockers'])
    with pytest.raises(MultiRefError) as caught:
        prepare(request(), object_info=info)
    assert caught.value.code == 'model_missing'


def test_validate_graph_blocks_cloud_nodes_and_broken_links():
    info = _fake_object_info()
    graph = build_graph(request())
    graph['cloud'] = {'class_type': 'ComfyCloudMiniMaxH3ImageToVideoNode', 'inputs': {}}
    with pytest.raises(MultiRefError) as caught:
        validate_graph(graph, info)
    assert caught.value.code == 'cloud_api_node_forbidden'

    graph = build_graph(request())
    graph['i2v']['inputs']['first_frame'] = ['missing-node', 0]
    with pytest.raises(MultiRefError) as caught:
        validate_graph(graph, info)
    assert caught.value.code == 'link_target_missing'


def test_contract_file_matches_builder_constants():
    contract_path = Path(__file__).resolve().parents[1] / 'contracts/h3-multiref-api.json'
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    assert contract['recipe'] == recipe.RECIPE_ID
    assert contract['kind'] == 'api_topology_contract'
    assert contract['models'] == {
        'still_unet': STILL_UNET,
        'video_unet': VIDEO_UNET,
        'clip': CLIP_MODEL,
        'video_vae': VIDEO_VAE,
        'audio_vae': AUDIO_VAE,
    }
    assert contract['limits']['max_ref_images'] == MAX_REF_IMAGES
    assert contract['limits']['max_pictures'] == MAX_PICTURES
    graph = build_graph(request())
    classes = {node['class_type'] for node in graph.values()}
    for node in contract['nodes'].values():
        assert node['class_type'] in classes


@pytest.mark.host_tool
def test_live_comfy_object_info_accepts_recipe():
    try:
        info = recipe.fetch_object_info()
        body = prepare(request(character_ref_images=['example.png', 'example.png'],
                               scene_image='example.png'), object_info=info)
    except MultiRefError as exc:
        pytest.skip(f'live ComfyUI unavailable: {exc.detail}')
    assert body['environment']['ready'] is True
    assert body['environment']['execution']['local_nodes_only'] is True
