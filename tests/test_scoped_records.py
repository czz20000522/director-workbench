from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from backend import app as backend
from test_production_presets import preset_client

REAL_THREAD = threading.Thread
CASES = [('reviews', {'asset_id': 'S01', 'stage': 'sample'}),
         ('checkpoints', {'asset_id': 'S01', 'stage_id': 'storyboard'}),
         ('artifacts', {'asset_id': 'S01', 'kind': 'storyboard'})]


@pytest.mark.parametrize('route,payload', CASES)
def test_record_scope_and_conflict(preset_client, route, payload):
    client, _ = preset_client
    assert client.post('/api/projects/create', json={'series': '测试', 'title': 'second', 'project_id': 'second'}).status_code == 200
    url = f'/api/projects/preset-test/{route}'
    assert client.post(url, json=payload).status_code == 422
    body = {**payload, 'expected_revision': 0, 'status': 'approved', 'source': 'test-agent'}
    saved = client.post(url, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] == 1
    conflict = client.post(url, json=body)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['current_revision'] == 1
    assert len(backend.project_records('preset-test')) == 1
    assert backend.project_records('second') == []
    assert client.get('/api/project').json()['id'] == 'second'
    assert client.post(url, json={**body, 'asset_id': 'S02'}).json()['revision'] == 1
    assert client.post(url, json={**body, 'expected_revision': 1}).json()['revision'] == 2
    assert client.post(f'/api/projects/missing/{route}', json=body).status_code == 404


@pytest.mark.parametrize('route,payload', CASES)
def test_concurrent_record_updates_accept_one_writer(preset_client, monkeypatch, route, payload):
    client, _ = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    barrier = threading.Barrier(2)
    def save(source):
        barrier.wait()
        return client.post(f'/api/projects/preset-test/{route}', json={**payload, 'expected_revision': 0, 'source': source})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ['human', 'agent']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(backend.project_records('preset-test')) == 1
