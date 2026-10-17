from backend import app as backend
from test_production_presets import preset_client, install, add_shot


def test_retry_returns_original_before_capacity_or_snapshot(preset_client, monkeypatch):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01')
    url = '/api/projects/preset-test/tasks'
    body = {'asset_id': 'S01', 'idempotency_key': 'first-attempt'}
    first = client.post(url, json=body)
    assert first.status_code == 200, first.text
    def must_not_run(*args, **kwargs):
        raise AssertionError('retry must not create another execution')
    monkeypatch.setattr(backend, 'require_submission_capacity', must_not_run)
    monkeypatch.setattr(backend, 'freeze_task_payload', must_not_run)
    retry = client.post(url, json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()['id'] == first.json()['id']
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1
    receipt = client.get('/api/projects/preset-test/submission-receipt', params={'key': body['idempotency_key']})
    assert receipt.status_code == 200
    assert receipt.json()['id'] == first.json()['id']
    absent = client.get('/api/projects/preset-test/submission-receipt', params={'key': 'not-yet-recorded'})
    assert absent.status_code == 404
    assert absent.json()['detail']['code'] == 'submission_not_found'
    conflict = client.post(url, json={**body, 'prompt': 'different'})
    assert conflict.status_code == 409
    assert conflict.json()['detail']['task_id'] == first.json()['id']


def test_different_keys_remain_distinct_attempts_after_pending_attempt_ends(preset_client):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01')
    first = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'first'}).json()
    duplicate = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'second'})
    assert duplicate.status_code == 409
    assert duplicate.json()['detail']['code'] == 'asset_task_pending'
    backend.set_task(first['id'], status='failed')
    second = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'second'}).json()
    tasks = [first, second]
    assert tasks[0]['id'] != tasks[1]['id']
    assert all(task['payload']['submission_receipt']['request']['asset_id'] == 'S01' for task in tasks)
