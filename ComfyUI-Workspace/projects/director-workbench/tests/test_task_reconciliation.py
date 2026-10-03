import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import app as backend


@pytest.fixture
def reconcile_client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr(backend, 'CURRENT_PROJECT_ID', 'test-project')
    monkeypatch.setattr(backend, 'CURRENT_PROJECT', {'id': 'test-project', 'assembly_asset_id': 'MASTER'})
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'result.mp4').write_bytes(b'output fixture')
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', output)
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'segments': [{'id': 'S01', 'prompt': 'retain director notes'}]}), encoding='utf-8')
    monkeypatch.setattr(backend, 'PLAN_PATH', plan)
    monkeypatch.setattr(backend, 'request_json', lambda path: {'queue_running': [], 'queue_pending': []})
    monkeypatch.setattr(backend, 'history_done', lambda prompt_id: {'status': 'success', 'outputs': ['result.mp4']})
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': True, 'gpu_free_mib': 1000, 'ram_free_gib': 8})
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(backend.threading, 'Thread', NoopThread)
    now = time.time()
    with backend.db() as connection:
        connection.execute('INSERT INTO tasks (id,asset_id,status,prompt_id,created_at,updated_at,payload,project_id,batch_id,sequence) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           ('old', 'S01', 'needs_reconcile', 'receipt', now, now, json.dumps({'asset_id': 'S01', 'workflow': 'frozen.json', 'plan_path': str(plan)}), 'test-project', 'batch', 1))
        connection.commit()
    return TestClient(backend.app), plan


def test_resume_restores_success_without_creating_task(reconcile_client):
    client, plan = reconcile_client
    response = client.post('/api/tasks/old/resume')
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'old'
    assert response.json()['status'] == 'succeeded'
    assert len(client.get('/api/tasks').json()) == 1
    segment = json.loads(plan.read_text(encoding='utf-8'))['segments'][0]
    assert segment['video']['path'] == 'output/result.mp4'
    assert segment['prompt'] == 'retain director notes'


@pytest.mark.parametrize('case', ['running', 'unknown', 'missing-output', 'no-receipt', 'newer-candidate'])
def test_uncertain_resume_never_duplicates(reconcile_client, monkeypatch, case):
    client, plan = reconcile_client
    if case == 'running': monkeypatch.setattr(backend, 'request_json', lambda path: {'queue_running': [[1, 'receipt']], 'queue_pending': []})
    if case == 'unknown': monkeypatch.setattr(backend, 'history_done', lambda prompt_id: None)
    if case == 'missing-output': monkeypatch.setattr(backend, 'history_done', lambda prompt_id: {'status': 'success', 'outputs': ['missing.mp4']})
    if case == 'no-receipt': backend.set_task('old', prompt_id=None)
    if case == 'newer-candidate': plan.write_text(json.dumps({'segments': [{'id': 'S01', 'comfyui_task_id': 'newer', 'video': {'path': 'newer.mp4'}}]}), encoding='utf-8')
    before = plan.read_bytes()
    assert client.post('/api/tasks/old/resume').status_code == 409
    assert len(client.get('/api/tasks').json()) == 1
    assert backend.get_task('old')['status'] == 'needs_reconcile'
    assert plan.read_bytes() == before


def test_confirmed_failure_can_retry_frozen_payload(reconcile_client, monkeypatch):
    client, _ = reconcile_client
    monkeypatch.setattr(backend, 'history_done', lambda prompt_id: {'status': 'error', 'outputs': []})
    response = client.post('/api/tasks/old/resume')
    assert response.status_code == 200, response.text
    assert response.json()['id'] != 'old'
    assert response.json()['payload'] == backend.get_task('old')['payload']
    assert backend.get_task('old')['status'] == 'failed'


def test_completed_batch_is_not_resubmitted(reconcile_client):
    client, _ = reconcile_client
    response = client.post('/api/batches/batch/resume')
    assert response.status_code == 409
    assert '全部完成' in response.json()['detail']
    assert backend.get_task('old')['status'] == 'succeeded'
    assert len(client.get('/api/tasks').json()) == 1


def test_new_submission_is_blocked_by_unresolved_receipt(reconcile_client):
    client, _ = reconcile_client
    assert client.post('/api/tasks', json={'asset_id': 'S01'}).status_code == 409
    assert len(client.get('/api/tasks').json()) == 1


@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_explicit_reconcile_never_creates_retry(reconcile_client, monkeypatch, outcome):
    client, plan = reconcile_client
    monkeypatch.setattr(backend, 'PROJECT', plan.parent)
    monkeypatch.setattr(backend, 'load_project_manifest', lambda _: {'id': 'test-project', 'plan_path': str(plan), 'assembly_asset_id': 'MASTER'})
    monkeypatch.setattr(backend, 'history_done', lambda _: {'status': outcome, 'outputs': ['result.mp4'] if outcome == 'success' else []})
    response = client.post('/api/projects/test-project/tasks/old/reconcile')
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'old'
    assert response.json()['status'] == ('succeeded' if outcome == 'success' else 'failed')
    assert len(client.get('/api/tasks').json()) == 1
    assert client.post('/api/projects/other/tasks/old/reconcile').status_code == 404
