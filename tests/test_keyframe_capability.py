"""关键帧能力判定。

11-07 起参考图模式不再依赖 Edit 2511，改走 Qwen Image 2.1 的多图参考，
所以这里的注册表样本必须来自 `tools.qwen_image_t2i.graph`。
旧断言里写死的 `qwen_image_edit_2511_int8_convrot.safetensors` 已经作废。
"""
from urllib.error import URLError

from backend import app as backend, keyframes
from tools.qwen_image_t2i import graph as text_graph

MAIN_MODEL = 'Qwen-Image-2.1-Turbo-AD-Q4_K.gguf'


def offline(*args, **kwargs):
    raise URLError(OSError(10061, 'refused'))


def registry() -> dict:
    info: dict = {}
    for node in text_graph('能力检查', 'keyframe', 0).values():
        fields = {key: [[value]] for key, value in node['inputs'].items()
                  if key in {'clip_name', 'unet_name', 'vae_name', 'lora_name'}}
        info[node['class_type']] = {'input': {'required': fields}}
    return info


def test_registry_must_include_nodes_and_exact_model_choices():
    info = registry()
    ready = keyframes.capability(info)
    assert ready['available'], ready
    assert ready['missing_nodes'] == [] and ready['missing_models'] == []

    removed = info.pop('VAEDecode')
    missing_node = keyframes.capability(info)
    assert not missing_node['available']
    assert missing_node['missing_nodes'] == ['VAEDecode']
    info['VAEDecode'] = removed

    info['UnetLoaderGGUF']['input']['required']['unet_name'] = [['different.gguf']]
    missing_model = keyframes.capability(info)
    assert not missing_model['available']
    assert missing_model['missing_models'] == [MAIN_MODEL]


def test_reference_mode_without_slots_creates_no_task(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', lambda *args: {})
    result = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'mode': 'reference', 'prompt': '画面', 'idempotency_key': 'no-slots'})
    # 没选任何参考槽就必须被拦下，不能悄悄排队一个空参考任务。
    assert result.status_code in (422, 503), result.text
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
