from pathlib import Path
from urllib.error import URLError

from backend import app as backend, keyframes
from tools.qwen_image_edit import graph


def offline(*args, **kwargs):
    raise URLError(OSError(10061, 'refused'))


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


def test_idle_engine_is_not_reported_as_missing_resources(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', offline)
    data = client.get('/api/keyframe-capability?mode=text').json()
    assert data['engine_offline'] is True
    assert data['available'] is False
    assert data['missing_nodes'] == [] and data['missing_models'] == []
    assert '引擎待启动' in data['reason']


def test_text_submission_is_accepted_while_engine_is_idle(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', offline)
    response = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'mode': 'text', 'prompt': '清晨的小镇', 'idempotency_key': 'idle-engine'})
    assert response.status_code == 202, response.text
