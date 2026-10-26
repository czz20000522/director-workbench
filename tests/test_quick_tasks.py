"""单步任务：隐藏容器、以提示词命名的列表，以及跨作品批量删除。"""
import json
from pathlib import Path

import pytest

from backend import app as backend


@pytest.fixture
def quick_client(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    return client, project


def _submit(client, prompt, seed=11):
    workspace = client.get('/api/quick-workspace').json()['project_id']
    response = client.post(f'/api/projects/{workspace}/keyframe-tasks', json={
        'mode': 'text', 'prompt': prompt, 'width': 768, 'height': 1024, 'seed': seed,
        'idempotency_key': f'quick-{seed}',
    })
    assert response.status_code == 202, response.text
    return response.json()


def test_quick_container_is_created_once_and_never_shows_as_a_work(quick_client):
    client, _ = quick_client
    first = client.get('/api/quick-workspace').json()['project_id']
    assert first == backend.QUICK_PROJECT_ID
    assert client.get('/api/quick-workspace').json()['project_id'] == first
    assert first not in [item['id'] for item in client.get('/api/projects').json()['projects']]
    assert 'quick-tasks' not in json.dumps(client.get('/api/projects').json(), ensure_ascii=False)
    assert (Path(backend.load_project_manifest(first)['workspace_root'].split('/')[-1]) == Path('_单步任务'))


def test_quick_tasks_are_titled_by_the_prompt_that_made_them(quick_client):
    client, _ = quick_client
    long_prompt = '我想生成一张雨夜里的城市街角，霓虹灯在积水里拉长，主角撑伞从画面右侧走入，' \
                  '镜头缓慢推进，背景有电车经过，色调偏冷但窗光偏暖，最后停在她的侧脸'
    _submit(client, long_prompt)
    _submit(client, '猫', seed=12)

    body = client.get('/api/quick-tasks').json()
    assert body['project_id'] == backend.QUICK_PROJECT_ID
    titles = {item['title'] for item in body['tasks']}
    assert '猫' in titles
    assert len(body['tasks']) == 2
    assert all(item['tool'] == '图片' for item in body['tasks'])
    assert all(item['deletable'] for item in body['tasks'])
    excerpt = next(item['title'] for item in body['tasks'] if item['title'] != '猫')
    assert excerpt.endswith('…') and len(excerpt) <= 47 and excerpt.startswith('我想生成一张雨夜里的城市街角')


def test_speech_tasks_are_named_by_the_line_or_the_designed_timbre(quick_client, monkeypatch):
    client, _ = quick_client
    monkeypatch.setattr(backend.speech, 'capability', lambda *args, **kwargs: {'available': True, 'missing': []})
    workspace = client.get('/api/quick-workspace').json()['project_id']
    reference = Path(backend.load_project_manifest(workspace)['workspace_root']) / 'voice.wav'
    reference.write_bytes(b'reference audio')
    endpoint = f'/api/projects/{workspace}/speech-tasks'
    for key, body in {
        'quick-timbre': {'engine': 'voxcpm-2', 'mode': 'design', 'text': '这段声音只用来试音色。',
                         'voice_description': '六十岁老先生，声音沙哑缓慢'},
        'quick-dialogue': {'engine': 'voxcpm-2', 'text': '别回头，先看完这段。', 'voice_reference': str(reference)},
    }.items():
        response = client.post(endpoint, json={**body, 'idempotency_key': key})
        assert response.status_code == 202, response.text
    cards = {item['title']: item['tool'] for item in client.get('/api/quick-tasks').json()['tasks']}
    # 音色设计 writes its own name into the list; an ordinary take stays 配音 and shows the line.
    assert cards['六十岁老先生，声音沙哑缓慢'] == '音色设计'
    assert cards['别回头，先看完这段。'] == '配音'


def test_batch_delete_previews_then_removes_rows_and_snapshots(quick_client):
    client, _ = quick_client
    first, second = _submit(client, '清晨的渔港', 21), _submit(client, '正午的沙漠', 22)
    snapshots = [Path(task['payload']['workflow']) for task in (first, second)]
    assert all(path.is_file() for path in snapshots)

    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [first['id'], second['id']]})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert {entry['task_id'] for entry in body['entries']} == {first['id'], second['id']}
    assert '不会被删除' in body['preserved']

    refused = client.post('/api/tasks/deletion', json={'plan_id': body['plan_id'], 'task_ids': [first['id'], second['id']]})
    assert refused.status_code == 422
    assert refused.json()['detail']['code'] == 'confirmation_required'
    assert all(path.is_file() for path in snapshots)

    applied = client.post('/api/tasks/deletion', json={
        'plan_id': body['plan_id'], 'task_ids': [first['id'], second['id']], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert sorted(applied.json()['deleted']) == sorted([first['id'], second['id']])
    assert not any(path.exists() for path in snapshots)
    assert client.get('/api/quick-tasks').json()['tasks'] == []
    assert client.get('/api/projects').json()['projects'][0]['id'] == 'preset-test'


def test_batch_delete_refuses_a_wider_scope_or_a_moving_status(quick_client):
    client, _ = quick_client
    task = _submit(client, '黄昏的天台', 31)
    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [task['id']]}).json()

    widened = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': [task['id'], '0' * 8], 'confirm': True,
    })
    assert widened.status_code == 409

    backend.set_task(task['id'], status='running', prompt_id='remote-9')
    stale = client.post('/api/tasks/deletion-plan', json={'task_ids': [task['id']]})
    assert stale.status_code == 409
    assert '停止' in stale.json()['detail']


def test_batch_delete_keeps_a_snapshot_still_used_by_another_task(quick_client):
    client, _ = quick_client
    kept = _submit(client, '共用快照的任务', 41)
    dropped = _submit(client, '要被删掉的任务', 42)
    shared = Path(kept['payload']['workflow'])
    # 两条任务指向同一份冻结快照时，删除其中一条不能把另一条的执行依据删掉。
    backend.set_task(dropped['id'], payload=json.dumps({
        **dropped['payload'], 'workflow': str(shared),
        'execution_snapshot': {**dropped['payload']['execution_snapshot'], 'api_graph': str(shared)},
    }, ensure_ascii=False))

    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [dropped['id']]}).json()
    applied = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': [dropped['id']], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert applied.json()['removed_snapshots'] == []
    assert shared.is_file()
    assert client.get(f'/api/projects/{backend.QUICK_PROJECT_ID}/tasks/{kept["id"]}').status_code == 200


def test_batch_plan_rejects_unknown_tasks_and_bad_ids(quick_client):
    client, _ = quick_client
    assert client.post('/api/tasks/deletion-plan', json={'task_ids': ['nope']}).status_code == 404
    assert client.post('/api/tasks/deletion', json={
        'plan_id': '../escape', 'task_ids': ['nope'], 'confirm': True,
    }).status_code == 422
