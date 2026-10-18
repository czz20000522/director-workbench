import json
import sqlite3
import threading
import time

from backend import app as backend
from support import add_shot, install


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


def test_sqlite_migration_keeps_oldest_duplicate_idempotency_row(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL, prompt_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, result TEXT, error TEXT, batch_id TEXT, sequence INTEGER, project_id TEXT)")
    payload = json.dumps({'submission_receipt': {'key': 'legacy-key', 'request': {'asset_id': 'S01'}}})
    connection.executemany("INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES(?,?,?,?,?,?,?)", [
        ('old', 'S01', 'failed', 1, 1, payload, 'project'),
        ('new', 'S01', 'failed', 2, 2, payload, 'project'),
    ])
    connection.commit(); connection.close()
    monkeypatch.setattr(backend, 'DB_PATH', path)
    with backend.db() as migrated:
        row = migrated.execute('SELECT task_id FROM task_idempotency WHERE project_id=? AND idempotency_key=?', ('project', 'legacy-key')).fetchone()
        assert row['task_id'] == 'old'
    assert backend.find_submission_task('project', 'legacy-key')['id'] == 'old'


def test_sqlite_unique_idempotency_trigger_allows_one_concurrent_insert(tmp_path, monkeypatch):
    path = tmp_path / 'concurrent.sqlite3'
    monkeypatch.setattr(backend, 'DB_PATH', path)
    barrier = threading.Barrier(2)
    outcomes = []

    def insert(task_id):
        try:
            with backend.db() as connection:
                barrier.wait()
                connection.execute("INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES(?,?,?,?,?,?,?)", (task_id, 'S01', 'scheduler_waiting', time.time(), time.time(), json.dumps({'submission_receipt': {'key': 'same'}}), 'project'))
                connection.commit()
            outcomes.append('ok')
        except sqlite3.IntegrityError:
            outcomes.append('duplicate')

    threads = [threading.Thread(target=insert, args=(task_id,)) for task_id in ('a', 'b')]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == ['duplicate', 'ok']
    with backend.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
