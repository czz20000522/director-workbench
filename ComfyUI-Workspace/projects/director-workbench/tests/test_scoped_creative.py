from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
from backend import app as backend
from test_production_presets import preset_client

REAL_THREAD = threading.Thread
URL = '/api/projects/preset-test/creative'


def test_agent_saves_unselected_project_and_requires_revision(preset_client):
    client, _ = preset_client
    assert client.post('/api/projects/create', json={'series': '测试', 'title': 'second', 'project_id': 'second'}).status_code == 200
    assert client.post(URL, json={'values': {'intent': 'missing revision'}}).status_code == 422
    response = client.post(URL, json={'expected_revision': 0, 'values': {'intent': 'Agent创作设定'}, 'source': 'test-agent'})
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 1 and response.json()['atomic']
    assert client.get('/api/project').json()['id'] == 'second'
    assert client.get('/api/projects/second/state').json()['creative'] is None
    state = client.get('/api/projects/preset-test/state').json()
    assert state['creative']['values']['intent'] == 'Agent创作设定'
    assert not state['checkpoints']
    assert client.post('/api/projects/missing/creative', json={'expected_revision': 0}).status_code == 404


def test_concurrent_creative_edits_only_one_revision_wins(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    barrier = threading.Barrier(2)
    def save(intent):
        barrier.wait()
        return client.post(URL, json={'expected_revision': 0, 'values': {'intent': intent}, 'status': 'approved'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ['human', 'agent']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    state = client.get('/api/projects/preset-test/state').json()
    assert state['creative']['revision'] == 1
    assert state['checkpoints']['intent']['revision'] == 1
    assert len([item for item in state['history'] if item['record_type'] == 'creative_settings']) == 1
    error = next(response.json()['detail'] for response in responses if response.status_code == 409)
    assert error['code'] == 'creative_revision_conflict' and not error['retryable']


def test_partial_write_error_rolls_back_creative_and_artifacts(preset_client, monkeypatch):
    _, _ = preset_client
    client = TestClient(backend.app, raise_server_exceptions=False)
    original = backend.append_project_record_in_connection
    def fail(connection, project_id, record_type, *args, **kwargs):
        if record_type == 'artifact:character':
            raise RuntimeError('controlled persistence failure')
        return original(connection, project_id, record_type, *args, **kwargs)
    monkeypatch.setattr(backend, 'append_project_record_in_connection', fail)
    response = client.post(URL, json={'expected_revision': 0, 'values': {'intent': 'draft', 'character': 'hero'}})
    assert response.status_code == 500
    assert backend.project_records('preset-test') == []
