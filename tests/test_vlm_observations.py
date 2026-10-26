"""画面观察：冻结帧与问题、只读回执、队列与恢复；绝不登记创作素材。"""
import base64
import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import vlm

ENDPOINT = '/api/projects/preset-test/vlm-tasks'
IMAGE_FACTS = {'known': True, 'width': 1280, 'height': 720, 'duration_seconds': None,
               'fps': None, 'audio_streams': 0}
VIDEO_FACTS = {'known': True, 'width': 576, 'height': 1024, 'duration_seconds': 6.0,
               'fps': 24, 'audio_streams': 1}


@pytest.fixture(autouse=True)
def ready(monkeypatch):
    monkeypatch.setattr(vlm, 'capability', lambda: {'available': True, 'missing': [], 'model': vlm.MODEL_ID,
                                                    'service': vlm.SERVICE_URL, 'reason': '', 'note': '',
                                                    'default_provider': 'local-ollama',
                                                    'providers': {'local-ollama': {'available': True, 'label': '本机 Ollama', 'model': vlm.MODEL_ID, 'service': vlm.SERVICE_URL},
                                                                  vlm.CLOUD_PROVIDER: {'available': False, 'label': 'DeepSeek 视觉', 'missing': ['DeepSeek API Key'], 'reason': '未配置 API Key'}}})
    monkeypatch.setattr(vlm, '_extract_frame', lambda source, frame_seconds, destination:
                        destination.write_bytes(b'fake jpeg frame bytes'))


def image_source(project):
    path = project / 'frame.png'
    path.write_bytes(b'png fixture bytes')
    return path


def video_source(project):
    path = project / 'shot.mp4'
    path.write_bytes(b'mp4 fixture bytes')
    return path


def submit(client, project, **extra):
    body = {'source': str(image_source(project)), 'prompt': '画面里有几个人？机位是什么？',
            'idempotency_key': 'vlm-one', **extra}
    response = client.post(ENDPOINT, json=body)
    assert response.status_code == 202, response.text
    return response.json()


def test_prepare_freezes_image_and_refuses_unauthorized_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    source = image_source(tmp_path)
    payload = vlm.prepare('task-1', source, tmp_path / 'tasks', prompt='这一段是什么景别？',
                          frame_seconds=None, temperature=0.8, num_predict=512)
    request, output = vlm.frozen_request(payload)
    assert request['source_kind'] == 'image'
    assert request['observation_frame'] == request['source']
    assert request['prompt'] == '这一段是什么景别？'
    assert Path(request['source']).read_bytes() == b'png fixture bytes'
    assert output == (tmp_path / 'tasks' / 'task-1' / 'result').resolve()


def test_prepare_requires_explicit_frame_for_video_and_rejects_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(VIDEO_FACTS))
    source = video_source(tmp_path)
    with pytest.raises(ValueError, match='取帧时间点'):
        vlm.prepare('task-2', source, tmp_path / 'tasks', prompt='看看构图', frame_seconds=None,
                    temperature=0.8, num_predict=512)
    with pytest.raises(ValueError, match='超出视频时长'):
        vlm.prepare('task-3', source, tmp_path / 'tasks', prompt='看看构图', frame_seconds=6.0,
                    temperature=0.8, num_predict=512)
    payload = vlm.prepare('task-4', source, tmp_path / 'tasks', prompt='看看构图', frame_seconds=2.5,
                          temperature=0.8, num_predict=512)
    request, _ = vlm.frozen_request(payload)
    assert request['source_kind'] == 'video' and request['frame_seconds'] == 2.5
    assert Path(request['observation_frame']).is_file()
    with pytest.raises(ValueError, match='取帧时间点'):
        vlm.prepare('task-5', image_source(tmp_path), tmp_path / 'tasks', prompt='看看',
                    frame_seconds=1.0, temperature=0.8, num_predict=512)


@pytest.mark.parametrize('extra', [
    {'prompt': '   '}, {'prompt': '问' * 2001}, {'temperature': 2.5}, {'temperature': True},
    {'num_predict': 8}, {'num_predict': 4096}, {'num_predict': 12.5},
])
def test_prepare_rejects_invalid_question_and_sampling(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    body = {'prompt': '画面里有几个人？', 'frame_seconds': None, 'temperature': 0.8, 'num_predict': 512, **extra}
    with pytest.raises(ValueError):
        vlm.prepare('task-1', image_source(tmp_path), tmp_path / 'tasks', **body)


def test_frozen_request_rejects_tampered_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    payload = vlm.prepare('task-1', image_source(tmp_path), tmp_path / 'tasks', prompt='景别？',
                          frame_seconds=None, temperature=0.8, num_predict=512)
    request_path = Path(payload['request_path'])
    changed = json.loads(request_path.read_text(encoding='utf-8'))
    changed['prompt'] = '被改过的问题'
    request_path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='快照'):
        vlm.frozen_request(payload)


class FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"error":"unreachable"}'

    def iter_lines(self):
        return iter(self._lines)


class FakeClient:
    """Stand in for httpx streaming: the workbench must never call a real Ollama in tests."""

    requests = []
    lines = [json.dumps({'response': '画面里', 'done': False}),
             json.dumps({'response': '有两个人。', 'done': False}),
             json.dumps({'response': '', 'done': True, 'done_reason': 'stop', 'eval_count': 7,
                         'eval_duration': 1200000000, 'total_duration': 1500000000,
                         'created_at': '2026-10-25T08:00:00Z'})]

    def __init__(self, *args, **kwargs):
        self.options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, json=None):
        type(self).requests.append({'method': method, 'url': url, 'body': json, 'client': self.options})
        return FakeResponse(type(self).lines)


def test_invoke_streams_text_and_sends_the_frozen_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    payload = vlm.prepare('task-1', image_source(tmp_path), tmp_path / 'tasks', prompt='景别？',
                          frame_seconds=None, temperature=0.6, num_predict=256)
    request, _ = vlm.frozen_request(payload)
    monkeypatch.setattr(vlm.httpx, 'Client', FakeClient)
    answer = vlm._invoke(request, lambda: False)
    sent = FakeClient.requests[-1]
    assert sent['method'] == 'POST' and sent['url'].endswith('/api/generate')
    assert sent['client']['trust_env'] is False
    body = sent['body']
    assert body['model'] == request['model']
    assert body['keep_alive'] == '0'
    assert base64.b64decode(body['images'][0]) == b'png fixture bytes'
    assert body['options'] == {'temperature': 0.6, 'num_predict': 256}
    assert answer['observation'] == '画面里有两个人。'
    assert answer['eval_count'] == 7


def test_invoke_stops_between_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    payload = vlm.prepare('task-1', image_source(tmp_path), tmp_path / 'tasks', prompt='景别？',
                          frame_seconds=None, temperature=0.8, num_predict=512)
    request, _ = vlm.frozen_request(payload)
    monkeypatch.setattr(vlm.httpx, 'Client', FakeClient)
    with pytest.raises(vlm.VlmStopped):
        vlm._invoke(request, lambda: True)


def test_execute_writes_verified_receipt_without_asset_registration(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    task = submit(client, project)
    monkeypatch.setattr(vlm, '_ensure_service_online', lambda: None)
    monkeypatch.setattr(vlm, '_service_models', lambda **kwargs: [vlm.MODEL_ID])
    monkeypatch.setattr(vlm, '_invoke', lambda request, cancelled: {
        'observation': '两个人，中景。', 'done_reason': 'stop', 'eval_count': 9,
        'eval_duration_ns': 1, 'total_duration_ns': 2, 'created_at': '2026-10-25T08:00:00Z'})
    backend.run_vlm_observation_task(task['id'], task['payload'])
    completed = backend.get_task(task['id'])
    assert completed['status'] == 'succeeded'
    assert completed['result']['observation'] == '两个人，中景。'
    assert completed['result']['prompt'] == task['payload']['request']['prompt']
    assert completed['payload']['execution_finished_at']
    assert Path(completed['payload']['output_dir'], 'receipt.json').is_file()
    # The invariant: an observation is evidence, not a creative candidate.
    assert [asset for asset in backend.require_project_manifest('preset-test')['assets']
            if str(asset.get('id', '')).startswith('vlm')] == []


def test_submission_freezes_question_and_rejects_changed_parameters(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    task = submit(client, project)
    assert task['payload']['kind'] == 'vlm_observation'
    assert task['payload']['request']['temperature'] == 0.8
    assert Path(task['payload']['request_path']).is_file()
    body = {'source': str(project / 'frame.png'), 'prompt': '画面里有几个人？机位是什么？', 'idempotency_key': 'vlm-one'}
    assert client.post(ENDPOINT, json=body).json()['id'] == task['id']
    assert client.post(ENDPOINT, json={**body, 'prompt': '换一个问法'}).status_code == 409
    assert client.post(ENDPOINT, json={**body, 'idempotency_key': 'other', 'num_predict': 4097}).status_code == 422
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_missing_service_rejects_before_creating_task(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm, 'capability', lambda: {'available': False, 'missing': ['Ollama 服务'], 'reason': 'offline',
                                                    'model': vlm.MODEL_ID, 'service': vlm.SERVICE_URL, 'note': '',
                                                    'providers': {'local-ollama': {'available': False, 'label': '本机 Ollama', 'reason': 'offline'},
                                                                  vlm.CLOUD_PROVIDER: {'available': False, 'label': 'DeepSeek 视觉', 'missing': ['DeepSeek API Key'], 'reason': '未配置 API Key'}}})
    response = client.post(ENDPOINT, json={'source': str(image_source(project)), 'prompt': '景别？',
                                          'idempotency_key': 'vlm-offline'})
    assert response.status_code == 503
    assert response.json()['detail']['missing'] == ['Ollama 服务']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_stop_and_recovery_never_rerun_the_model(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    monkeypatch.setattr(vlm, '_ensure_service_online', lambda: None)
    monkeypatch.setattr(vlm, '_service_models', lambda **kwargs: [vlm.MODEL_ID])
    task = submit(client, project)
    receipt = {**task['payload']['request'], 'status': 'succeeded', 'observation': '已完成但被停止。',
               'done_reason': 'stop', 'eval_count': 3, 'eval_duration_ns': 1, 'total_duration_ns': 2,
               'created_at': '2026-10-25T08:00:00Z'}
    (Path(task['payload']['output_dir'])).mkdir(parents=True, exist_ok=True)
    Path(task['payload']['output_dir'], 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
    backend.set_task(task['id'], stop_requested=1)

    def stopped(payload, cancelled):
        assert cancelled()
        raise vlm.VlmStopped('观察任务已停止')
    monkeypatch.setattr(vlm, 'execute', stopped)
    backend.run_vlm_observation_task(task['id'], task['payload'])
    assert client.get(f"/api/projects/preset-test/tasks/{task['id']}").json()['status'] == 'stopped'
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    resumed = client.post(detail + '/resume')
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['status'] == 'stopped'
    backend.set_task(task['id'], status='needs_reconcile', stop_requested=0, error=None)
    calls = []
    monkeypatch.setattr(vlm, 'execute', lambda *args, **kwargs: calls.append('execute'))
    recovered = client.post(detail + '/reconcile')
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'succeeded'
    assert recovered.json()['result']['observation'] == '已完成但被停止。'
    assert calls == []


REAL_CAPABILITY = vlm.capability


def test_stopped_ollama_with_installed_model_stays_usable(monkeypatch, tmp_path):
    manifest = tmp_path / 'manifests' / 'registry.ollama.ai' / 'library' / 'qwen2.5vl' / '7b'
    manifest.mkdir(parents=True)
    monkeypatch.setenv('OLLAMA_MODELS', str(tmp_path))
    monkeypatch.setattr(vlm, '_service_models', lambda **kwargs: (_ for _ in ()).throw(OSError('connection refused')))

    monkeypatch.setattr(vlm, '_ollama_executable', lambda: 'D:/Ollama/ollama.exe')
    state = REAL_CAPABILITY()['providers']['local-ollama']
    assert state['available'] is True and state['running'] is False and state['missing'] == []
    assert '自动拉起' in state['reason']

    monkeypatch.setattr(vlm, '_ollama_executable', lambda: '')
    state = REAL_CAPABILITY()['providers']['local-ollama']
    assert state['available'] is False and 'Ollama 可执行文件' in state['missing']

    manifest.rename(tmp_path / 'elsewhere')
    monkeypatch.setattr(vlm, '_ollama_executable', lambda: 'D:/Ollama/ollama.exe')
    state = REAL_CAPABILITY()['providers']['local-ollama']
    assert state['available'] is False and any('模型' in item for item in state['missing'])

    monkeypatch.setattr(vlm, '_service_models', lambda **kwargs: [vlm.MODEL_ID])
    state = REAL_CAPABILITY()['providers']['local-ollama']
    assert state == {**state, 'available': True, 'running': True, 'missing': []}


def test_failure_and_dispatch_route_to_the_vlm_runner(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    task = submit(client, project)

    def fail(payload, cancelled):
        raise vlm.VlmFailed('Ollama 拒绝观察请求（HTTP 500）')
    monkeypatch.setattr(vlm, 'execute', fail)
    runner = backend.run_vlm_observation_task
    dispatched = []
    monkeypatch.setattr(backend, 'run_vlm_observation_task', lambda task_id, payload: dispatched.append(task_id))
    backend.execute_scheduled_task(backend.get_task(task['id']))
    assert dispatched == [task['id']]
    runner(task['id'], task['payload'])
    failed = backend.get_task(task['id'])
    assert failed['status'] == 'failed' and 'HTTP 500' in failed['error']
    assert client.get('/api/projects/preset-test').json()['assets'] == []


class FakeCloudResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"error":{"message":"Content Exists Risk"}}'

    def iter_lines(self):
        return iter(self._lines)


class FakeCloudClient:
    calls = []
    lines = ['data: ' + json.dumps({'choices': [{'delta': {'content': '中景'}}]}),
             'data: ' + json.dumps({'choices': [{'delta': {'content': '，两个人。'}}], 'usage': {'prompt_tokens': 400, 'completion_tokens': 9}}),
             'data: [DONE]']

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, json=None, headers=None):
        type(self).calls.append({'method': method, 'url': url, 'body': json, 'headers': headers, 'client': self.kwargs})
        return FakeCloudResponse(type(self).lines)


def prepared_cloud_request(tmp_path, monkeypatch):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    monkeypatch.setattr(vlm, 'cloud_config', lambda: {'api_key': 'sk-test-key', 'base_url': 'https://api.deepseek.com',
                                                      'model': 'deepseek-flash'})
    payload = vlm.prepare('task-cloud', image_source(tmp_path), tmp_path / 'tasks', prompt='几个人？',
                          frame_seconds=None, temperature=0.7, num_predict=256, provider=vlm.CLOUD_PROVIDER)
    request, _ = vlm.frozen_request(payload)
    return request


def test_cloud_provider_freezes_endpoint_without_freezing_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    monkeypatch.setattr(vlm, 'cloud_config', lambda: {'api_key': '', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash'})
    with pytest.raises(ValueError, match='API Key'):
        vlm.prepare('task-x', image_source(tmp_path), tmp_path / 'tasks', prompt='几个人？', frame_seconds=None,
                    temperature=0.8, num_predict=512, provider=vlm.CLOUD_PROVIDER)
    with pytest.raises(ValueError, match='观察来源'):
        vlm.prepare('task-y', image_source(tmp_path), tmp_path / 'tasks', prompt='几个人？', frame_seconds=None,
                    temperature=0.8, num_predict=512, provider='openai-vision')
    request = prepared_cloud_request(tmp_path, monkeypatch)
    assert request['provider'] == vlm.CLOUD_PROVIDER and request['model'] == 'deepseek-flash'
    assert request['service'] == 'https://api.deepseek.com'
    assert 'api_key' not in json.dumps(request, ensure_ascii=False)


def test_cloud_invoke_streams_openai_style_chunks(tmp_path, monkeypatch):
    request = prepared_cloud_request(tmp_path, monkeypatch)
    monkeypatch.setattr(vlm.httpx, 'Client', FakeCloudClient)
    answer = vlm._invoke(request, lambda: False)
    sent = FakeCloudClient.calls[-1]
    assert sent['url'] == 'https://api.deepseek.com/chat/completions'
    assert sent['headers']['Authorization'] == 'Bearer sk-test-key'
    content = sent['body']['messages'][0]['content']
    assert content[0] == {'type': 'text', 'text': '几个人？'}
    assert content[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert sent['body']['stream'] is True and sent['body']['max_tokens'] == 256
    assert answer['observation'] == '中景，两个人。'
    assert answer['eval_count'] == 9 and answer['prompt_tokens'] == 400


def test_cloud_content_policy_block_is_actionable(tmp_path, monkeypatch):
    request = prepared_cloud_request(tmp_path, monkeypatch)

    class Blocked(FakeCloudClient):
        def stream(self, method, url, json=None, headers=None):
            response = FakeCloudResponse([])
            response.status_code = 400
            type(self).calls.append({'body': json})
            return response
    monkeypatch.setattr(vlm.httpx, 'Client', Blocked)
    with pytest.raises(vlm.VlmFailed, match='请改用本机观察'):
        vlm._invoke(request, lambda: False)


def test_cloud_submission_is_a_different_identity_and_reports_unavailability(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(vlm.creative_inspection, 'probe', lambda path: dict(IMAGE_FACTS))
    body = {'source': str(image_source(project)), 'prompt': '几个人？', 'idempotency_key': 'vlm-local'}
    local = client.post(ENDPOINT, json=body)
    assert local.status_code == 202, local.text
    assert local.json()['payload']['request']['provider'] == 'local-ollama'
    # A different provider is a different request under the same key, and the cloud path is
    # still refused before anything is queued while no key is configured.
    assert client.post(ENDPOINT, json={**body, 'provider': vlm.CLOUD_PROVIDER}).status_code == 409
    blocked = client.post(ENDPOINT, json={**body, 'idempotency_key': 'vlm-cloud-off', 'provider': vlm.CLOUD_PROVIDER})
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()['detail']['missing'] == ['DeepSeek API Key']
    monkeypatch.setattr(vlm, 'cloud_config', lambda: {'api_key': 'sk-test', 'base_url': 'https://api.deepseek.com',
                                                      'model': 'deepseek-flash'})
    monkeypatch.setattr(vlm, 'capability', lambda: {'available': True, 'missing': [], 'model': vlm.MODEL_ID,
                                                    'service': vlm.SERVICE_URL, 'reason': '', 'note': '',
                                                    'providers': {'local-ollama': {'available': True, 'label': '本机 Ollama', 'missing': []},
                                                                  vlm.CLOUD_PROVIDER: {'available': True, 'label': 'DeepSeek 视觉', 'missing': []}}})
    cloud = client.post(ENDPOINT, json={**body, 'provider': vlm.CLOUD_PROVIDER})
    assert cloud.status_code == 409, cloud.text
    frozen = client.post(ENDPOINT, json={**body, 'idempotency_key': 'vlm-cloud', 'provider': vlm.CLOUD_PROVIDER})
    assert frozen.status_code == 202, frozen.text
    assert frozen.json()['payload']['request']['service'] == 'https://api.deepseek.com'
