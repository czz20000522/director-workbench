"""队列读取与任务整理：跨作品列表、派发前修改、改参数重跑、确认式删除。"""
import json
from pathlib import Path

import pytest

from backend import app as backend


@pytest.fixture
def queue_client(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    response = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'mode': 'text', 'prompt': '清晨的小镇', 'width': 768, 'height': 1344,
        'seed': 7, 'idempotency_key': 'queue-proof',
    })
    assert response.status_code == 202, response.text
    return client, project, response.json()


def _payload(client, task_id):
    return client.get(f'/api/projects/preset-test/tasks/{task_id}').json()['payload']


def test_queue_lists_rows_with_their_available_actions(queue_client):
    client, _, task = queue_client
    body = client.get('/api/queue/tasks').json()
    assert body['gpu_slots'] == 1
    assert body['status_counts'].get('scheduler_waiting') == 1
    row = next(item for item in body['tasks'] if item['id'] == task['id'])
    assert row['project_title'] == '新作'
    assert row['kind'] == 'keyframe'
    assert row['prompt'] == '清晨的小镇'
    assert row['seed'] == 7 and row['width'] == 768
    assert row['deletable'] is True and row['editable'] is True
    assert client.get('/api/queue/tasks?state=terminal').json()['tasks'] == []
    assert client.get('/api/queue/tasks?project_id=other').json()['tasks'] == []


def test_edit_before_dispatch_refreezes_the_workflow(queue_client):
    client, _, task = queue_client
    frozen = Path(task['payload']['workflow'])
    assert frozen.is_file()

    response = client.patch('/api/projects/preset-test/tasks/' + task['id'],
                            json={'prompt': '正午的集市', 'seed': 21})
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited['payload']['revision'] == 1
    assert edited['payload']['request']['prompt'] == '正午的集市'
    assert edited['payload']['request']['seed'] == 21
    assert edited['payload']['edited']['fields'] == ['prompt', 'seed']
    assert not frozen.exists(), '旧的冻结快照必须随修改消失'
    assert Path(edited['payload']['workflow']).is_file()
    assert json.loads(Path(edited['payload']['workflow']).read_text(encoding='utf-8'))['4']['inputs']['prompt'] == '正午的集市'
    # 幂等键仍指向这条任务，但记录的是改后的参数，重放不会误报冲突。
    assert client.get('/api/projects/preset-test/submission-receipt?key=queue-proof').json()['id'] == task['id']
    receipt = _payload(client, task['id'])['submission_receipt']['request']
    assert receipt['prompt'] == '正午的集市' and receipt['seed'] == 21


def test_edit_refuses_once_the_task_has_been_dispatched(queue_client):
    client, _, task = queue_client
    backend.set_task(task['id'], status='running', prompt_id='remote-1')
    response = client.patch('/api/projects/preset-test/tasks/' + task['id'], json={'prompt': '换个说法'})
    assert response.status_code == 409
    assert '冻结' in response.json()['detail']
    assert _payload(client, task['id'])['request']['prompt'] == '清晨的小镇'


def test_edit_rejects_a_prompt_about_minors_or_real_people(queue_client):
    client, _, task = queue_client
    response = client.patch('/api/projects/preset-test/tasks/' + task['id'], json={'prompt': '未成年人肖像'})
    assert response.status_code == 422
    assert '本机自用也不放行' in response.json()['detail']


def test_revise_derives_a_new_task_and_keeps_the_original(queue_client):
    client, _, task = queue_client
    response = client.post(f'/api/projects/preset-test/tasks/{task["id"]}/revise', json={'prompt': '黄昏的河岸'})
    assert response.status_code == 200, response.text
    derived = response.json()
    assert derived['id'] != task['id']
    assert derived['status'] == 'scheduler_waiting'
    assert derived['payload']['retry_of'] == task['id']
    assert derived['payload']['request']['prompt'] == '黄昏的河岸'
    assert derived['payload']['request']['seed'] == 7, '未修改的字段沿用原值'
    assert _payload(client, task['id'])['request']['prompt'] == '清晨的小镇'
    assert len(client.get('/api/queue/tasks').json()['tasks']) == 2


def test_delete_requires_confirmation_then_clears_rows_and_snapshot(queue_client, tmp_path):
    client, project, task = queue_client
    frozen = Path(task['payload']['workflow'])
    output = tmp_path / 'kept.png'
    output.write_bytes(b'png')
    backend.set_task(task['id'], status='succeeded', result=json.dumps({'output': str(output)}))

    refused = client.request('DELETE', '/api/projects/preset-test/tasks/' + task['id'], json={})
    assert refused.status_code == 422
    assert refused.json()['detail']['code'] == 'confirmation_required'
    assert frozen.is_file()

    applied = client.request('DELETE', '/api/projects/preset-test/tasks/' + task['id'], json={'confirm': True})
    assert applied.status_code == 200, applied.text
    assert applied.json()['deleted'] == task['id']
    assert str(frozen) in applied.json()['removed_snapshots']
    assert not frozen.exists()
    assert output.exists(), 'ComfyUI 产物不属于删除范围'
    assert client.get('/api/queue/tasks').json()['tasks'] == []
    with backend.DB_LOCK, backend.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM resource_samples WHERE task_id = ?', (task['id'],)).fetchone()[0] == 0


def test_delete_refuses_a_running_or_batch_queued_task(queue_client):
    client, _, task = queue_client
    backend.set_task(task['id'], status='running', prompt_id='remote-1')
    blocked = client.request('DELETE', '/api/projects/preset-test/tasks/' + task['id'], json={'confirm': True})
    assert blocked.status_code == 409
    assert '停止' in blocked.json()['detail']

    backend.set_task(task['id'], status='batch_waiting', prompt_id=None, batch_id='batch-1')
    batch = client.request('DELETE', '/api/projects/preset-test/tasks/' + task['id'], json={'confirm': True})
    assert batch.status_code == 409
    assert '批次' in batch.json()['detail']
    assert client.get(f'/api/projects/preset-test/tasks/{task["id"]}').status_code == 200


def test_queue_actions_reject_foreign_projects_and_missing_tasks(queue_client):
    client, _, task = queue_client
    assert client.request('DELETE', f'/api/projects/nope/tasks/{task["id"]}', json={'confirm': True}).status_code == 404
    assert client.patch('/api/projects/preset-test/tasks/' + '0' * 8, json={'prompt': 'x'}).status_code == 404
    assert client.post(f'/api/projects/preset-test/tasks/{task["id"]}/revise', json={}).status_code == 422
