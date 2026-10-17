from pathlib import Path

from backend import app as backend, keyframes
from tools.qwen_image_edit import graph
from test_production_presets import preset_client


def test_registry_must_include_nodes_and_exact_model_choices():
    info = {}
    for node in graph(Path('reference.png'), '', 'test', 0).values():
        fields = {key: [[value]] for key, value in node['inputs'].items()
                  if key in {'clip_name', 'unet_name', 'vae_name'}}
        info[node['class_type']] = {'input': {'required': fields}}
    assert keyframes.capability(info)['available']
    image_scale = info.pop('ImageScale')
    assert keyframes.capability(info)['missing_nodes'] == ['ImageScale']
    info['ImageScale'] = image_scale
    info.pop('SaveImage')
    info['UNETLoader']['input']['required']['unet_name'] = [['different.safetensors']]
    result = keyframes.capability(info)
    assert not result['available']
    assert result['missing_nodes'] == ['SaveImage']
    assert result['missing_models'] == ['qwen_image_edit_2511_int8_convrot.safetensors']


def test_unavailable_registry_creates_no_task(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', lambda *args: {})
    assert not client.get('/api/keyframe-capability').json()['available']
    result = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'prompt': '画面', 'reference_image': 'missing.png', 'idempotency_key': 'missing-models'})
    assert result.status_code == 503
    assert 'SaveImage' in result.json()['detail']['missing_nodes']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
