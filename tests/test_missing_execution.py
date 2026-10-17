import json

import pytest

from backend import app as backend
from test_production_presets import preset_client


@pytest.fixture
def missing_task(preset_client):
    client, _ = preset_client
    with backend.db() as connection:
        connection.execute("INSERT INTO tasks (id,asset_id,status,prompt_id,created_at,updated_at,payload,result,project_id) VALUES (?,?,?,?,?,?,?,?,?)",
                           ('lost', 'S01', 'needs_reconcile', 'original', 1, 1,
                            json.dumps({'submission_receipt': {'key': 'original-key'}}),
                            json.dumps({'submission': {'prompt_id': 'original'}}), 'preset-test'))
        connection.commit()
    return client


def test_resolution_preserves_original_receipt_and_never_submits(missing_task, monkeypatch):
    calls = []
    def read(path):
        calls.append(path)
        return {'queue_running': [], 'queue_pending': []} if path == '/queue' else {}
    monkeypatch.setattr(backend, 'request_json', read)
    response = missing_task.post('/api/projects/preset-test/tasks/lost/resolve-missing', json={
        'confirm_execution_ended': True, 'note': '旧进程已退出，检查输出没有视频',
    })
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['status'] == 'failed'
    assert task['prompt_id'] == 'original'
    assert task['payload']['submission_receipt']['key'] == 'original-key'
    assert task['result']['submission']['prompt_id'] == 'original'
    assert task['result']['missing_execution_resolution']['note'] == '旧进程已退出，检查输出没有视频'
    assert calls == ['/queue', '/history/original']
    with backend.db() as connection:
        assert connection.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('queue,history', [
    ({'queue_running': [[0, 'original']], 'queue_pending': []}, {}),
    ({'queue_running': [], 'queue_pending': []}, {'original': {'status': 'success'}}),
    ({}, {}),
])
def test_resolution_rejects_unverified_or_recoverable_execution(missing_task, monkeypatch, queue, history):
    monkeypatch.setattr(backend, 'request_json', lambda path: queue if path == '/queue' else history)
    response = missing_task.post('/api/projects/preset-test/tasks/lost/resolve-missing', json={
        'confirm_execution_ended': True, 'note': 'checked',
    })
    assert response.status_code in (409, 503)
    assert backend.get_task('lost')['status'] == 'needs_reconcile'


@pytest.mark.parametrize('body', [{}, {'confirm_execution_ended': False, 'note': 'checked'},
                                  {'confirm_execution_ended': True, 'note': '  '}])
def test_resolution_requires_explicit_confirmation_and_note(missing_task, body):
    response = missing_task.post('/api/projects/preset-test/tasks/lost/resolve-missing', json=body)
    assert response.status_code == 422
    assert backend.get_task('lost')['status'] == 'needs_reconcile'
