import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from backend import app as backend
from test_production_presets import preset_client

REAL_THREAD = threading.Thread


def prepare(client):
    assert client.post('/api/projects/create', json={'series': '测试', 'title': '另一个作品', 'project_id': 'second'}).status_code == 200
    for project_id in ('preset-test', 'second'):
        assert client.post(f'/api/projects/{project_id}/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5, 'prompt': project_id}).status_code == 200
        backend.append_project_record('creative_settings', {'values': {'intent': project_id}}, project_id=project_id)
        with backend.db() as connection:
            for index in range(3):
                connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)', (f'{project_id}-{index}', 'S01', 'succeeded', index, index, '{}', project_id))
            connection.commit()


def test_scoped_reads_do_not_change_selection_or_mix_projects(preset_client, monkeypatch):
    client, _ = preset_client
    prepare(client)
    selected_before = backend.CURRENT_PROJECT_ID
    catalog_before = backend.PROJECT_CATALOG_PATH.read_bytes()
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)

    def read(project_id):
        # Concurrent clients share one application, not ten independent server
        # lifespans/schedulers. These calls only exercise scoped HTTP reads.
        reader=TestClient(backend.app)
        try:
            assert reader.get(f'/api/projects/{project_id}').json()['id'] == project_id
            assert reader.get(f'/api/projects/{project_id}/plan').json()['segments'][0]['prompt'] == project_id
            assert reader.get(f'/api/projects/{project_id}/state').json()['creative']['values']['intent'] == project_id
            tasks = reader.get(f'/api/projects/{project_id}/tasks').json()['tasks']
            assert len(tasks) == 3 and all(task['project_id'] == project_id for task in tasks)
        finally:
            reader.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read, ['preset-test', 'second'] * 5))
    assert backend.CURRENT_PROJECT_ID == selected_before
    assert backend.PROJECT_CATALOG_PATH.read_bytes() == catalog_before
    assert client.get('/api/project').json()['id'] == selected_before


def test_task_scope_pagination_and_missing_projects(preset_client):
    client, _ = preset_client
    prepare(client)
    first = client.get('/api/projects/preset-test/tasks?limit=2').json()
    assert [task['id'] for task in first['tasks']] == ['preset-test-2', 'preset-test-1']
    assert first['next_offset'] == 2
    last = client.get('/api/projects/preset-test/tasks?limit=2&offset=2').json()
    assert [task['id'] for task in last['tasks']] == ['preset-test-0']
    assert last['next_offset'] is None
    assert client.get('/api/projects/preset-test/tasks/preset-test-0').status_code == 200
    assert client.get('/api/projects/second/tasks/preset-test-0').status_code == 404
    assert client.get('/api/projects/preset-test/tasks?limit=101').status_code == 422
    for suffix in ('', '/plan', '/state', '/tasks', '/tasks/preset-test-0'):
        assert client.get('/api/projects/missing' + suffix).status_code == 404


def test_new_project_empty_plan_and_openapi_discovery(preset_client):
    client, _ = preset_client
    assert client.get('/api/projects/preset-test/plan').json()['segments'] == []
    paths = client.get('/openapi.json').json()['paths']
    for suffix in ('', '/plan', '/state', '/tasks', '/tasks/{task_id}'):
        assert 'get' in paths['/api/projects/{project_id}' + suffix]
