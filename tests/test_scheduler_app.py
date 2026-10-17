"""Real public routes with synthetic private accounts and fake executors only."""
import json
import threading

import pytest

from backend import app as backend
from backend.task_scheduler import TaskScheduler
from test_private_workbench import private_workbench, create


@pytest.fixture
def queued_private_app(private_workbench, monkeypatch):
    a, b, ownership, project = private_workbench
    def fake_comfy(path, payload=None, **kwargs):
        if path == '/queue':
            return {'queue_running': [], 'queue_pending': []}
        if path.startswith('/history'):
            return {}
        if path == '/system_stats':
            return {'devices': [{'vram_free': 8 * 2 ** 30, 'vram_total': 16 * 2 ** 30}], 'system': {'ram_free': 8 * 2 ** 30}}
        raise AssertionError('Unexpected real GPU/model operation: ' + path)
    monkeypatch.setattr(backend, 'request_json', fake_comfy)
    monkeypatch.setattr(backend, 'REFERENCE_DECOMPOSE_ACTIVE', 0)
    executed = []
    def fake_workflow(task_id, payload):
        executed.append((payload['owner_user'], payload['asset_id']))
        backend.set_task(task_id, status='succeeded')
    monkeypatch.setattr(backend, 'run_workflow_submission', fake_workflow)
    scheduler = TaskScheduler(lambda: backend.db(), backend.DB_LOCK, backend.GPU_ADMISSION_LOCK,
                              lambda: backend.scheduler_resource_reason(),
                              lambda task: backend.revalidate_queued_task(task),
                              lambda task: backend.execute_scheduled_task(task),
                              note_activity=lambda: backend.GPU_IDLE_RELEASER.note_activity())
    monkeypatch.setattr(backend, 'TASK_SCHEDULER', scheduler)
    yield a, b, ownership, scheduler, executed
    scheduler.stop()


def prepare(client, shots):
    project_id = create(client)['id']
    base = '/api/projects/' + project_id
    result = client.post(base + '/production-presets/h3-first-native-landscape')
    assert result.status_code == 200, result.text
    uploaded = client.post(base + '/upload', files=[('files', ('first.png', b'fixture-image', 'image/png'))])
    assert uploaded.status_code == 200, uploaded.text
    reference = uploaded.json()['uploaded'][0]
    for shot in shots:
        result = client.post(base + '/plan/segments', json={'segment_id': shot, 'duration_seconds': 5,
                                                          'prompt': shot + ' moves', 'first_frame': reference})
        assert result.status_code == 200, result.text
    return project_id, base


def test_private_http_batches_dispatch_fairly_from_frozen_owned_requests(queued_private_app):
    a, b, _, scheduler, executed = queued_private_app
    aid, abase = prepare(a, ['S01', 'S02', 'S03'])
    bid, bbase = prepare(b, ['S01', 'S02'])
    first = a.post(abase + '/batches', json={'asset_ids': ['S01', 'S02', 'S03'], 'idempotency_key': 'a-batch'})
    second = b.post(bbase + '/batches', json={'asset_ids': ['S01', 'S02'], 'idempotency_key': 'b-batch'})
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    for _ in range(5):
        scheduler.tick(threaded=False)
    assert executed == [('user001', 'S01'), ('user002', 'S01'), ('user001', 'S02'), ('user002', 'S02'), ('user001', 'S03')]
    for client, base, response, owner in ((a, abase, first, 'user001'), (b, bbase, second, 'user002')):
        for task_id in response.json()['task_ids']:
            task = client.get(base + '/tasks/' + task_id).json()
            assert task['status'] == 'succeeded'
            assert task['payload']['owner_user'] == owner
            assert task['payload']['execution_snapshot']['api_graph']


def test_http_duplicate_key_receipt_and_conflict_never_dispatch_twice(queued_private_app):
    a, _, _, scheduler, executed = queued_private_app
    _, base = prepare(a, ['S01', 'S02'])
    request = {'asset_id': 'S01', 'idempotency_key': 'same-request'}
    first = a.post(base + '/tasks', json=request)
    repeated = a.post(base + '/tasks', json=request)
    assert first.status_code == repeated.status_code == 200, (first.text, repeated.text)
    assert first.json()['id'] == repeated.json()['id']
    conflict = a.post(base + '/tasks', json={**request, 'asset_id': 'S02'})
    assert conflict.status_code == 409
    assert conflict.json()['detail']['code'] == 'idempotency_key_conflict'
    scheduler.tick(threaded=False)
    assert scheduler.tick(threaded=False) == []
    assert executed == [('user001', 'S01')]


def test_http_waiting_queue_and_cancel_are_private(queued_private_app):
    a, b, _, scheduler, executed = queued_private_app
    _, abase = prepare(a, ['S01'])
    _, bbase = prepare(b, ['S01'])
    first = a.post(abase + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'A'}).json()
    second = b.post(bbase + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'B'}).json()
    public = a.get('/api/queue')
    assert public.status_code == 200, public.text
    assert [task['id'] for task in public.json()['tasks']] == [first['id']]
    assert 'user002' not in public.text and second['id'] not in public.text
    assert b.get(abase + '/tasks/' + first['id']).status_code == 404
    assert b.post(abase + '/tasks/' + first['id'] + '/stop').status_code == 404
    stopped = a.post(abase + '/tasks/' + first['id'] + '/stop')
    assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped', stopped.text
    scheduler.tick(threaded=False)
    assert executed == [('user002', 'S01')]


def test_external_comfy_queue_keeps_public_request_waiting(queued_private_app, monkeypatch):
    a, _, _, scheduler, executed = queued_private_app
    _, base = prepare(a, ['S01'])
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: {'queue_running': [[0, 'external']], 'queue_pending': []})
    response = a.post(base + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'external-busy'})
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert scheduler.tick(threaded=False) == []
    readback = a.get(base + '/tasks/' + task_id).json()
    assert readback['queue']['state'] == 'waiting_resource'
    assert readback['queue']['reason']['code'] == 'external_queue_busy'
    assert executed == []


def test_http_overload_is_structured_and_preserves_existing_waits(queued_private_app):
    a, b, _, scheduler, _ = queued_private_app
    _, abase = prepare(a, ['S01', 'S02'])
    _, bbase = prepare(b, ['S01'])
    scheduler.global_limit = 2
    scheduler.user_limit = 1
    assert a.post(abase + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'A1'}).status_code == 200
    limited = a.post(abase + '/tasks', json={'asset_id': 'S02', 'idempotency_key': 'A2'})
    assert limited.status_code == 429, limited.text
    assert limited.json()['detail']['code'] == 'queue_user_limit'
    assert b.post(bbase + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'B1'}).status_code == 200
    assert len(a.get('/api/queue').json()['tasks']) == 1


def test_cpu_http_jobs_can_run_while_gpu_needs_reconcile(queued_private_app, monkeypatch):
    a, _, _, scheduler, executed = queued_private_app
    _, base = prepare(a, ['S01'])
    gpu = a.post(base + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'gpu'}).json()
    backend.set_task(gpu['id'], status='needs_reconcile')
    monkeypatch.setattr(backend.media_operations, 'capability', lambda: {'operations': {'silence': {'available': True}}})
    cpu = a.post(base + '/media-tasks', json={'operation': 'silence', 'duration_seconds': 1.0, 'idempotency_key': 'cpu'})
    assert cpu.status_code == 202, cpu.text
    completed_cpu = []
    def fake_cpu(task_id, payload):
        completed_cpu.append(task_id)
        backend.set_task(task_id, status='succeeded')
    monkeypatch.setattr(backend, 'run_media_operation_task', fake_cpu)
    assert scheduler.tick(threaded=False) == [cpu.json()['id']]
    assert completed_cpu == [cpu.json()['id']]
    assert executed == []
    assert a.get(base + '/tasks/' + gpu['id']).json()['status'] == 'needs_reconcile'


def test_idle_release_guard_distinguishes_runnable_and_dependency_wait(queued_private_app):
    a, _, _, scheduler, _ = queued_private_app
    _, base = prepare(a, ['S01', 'S02'])
    task = a.post(base + '/tasks', json={'asset_id': 'S01', 'idempotency_key': 'ready'}).json()
    assert backend.gpu_idle_reason() == '已有可派发 GPU 任务等待资源'
    with backend.DB_LOCK, backend.db() as connection:
        payload = task['payload']
        payload['segment_dependencies'] = ['S02']
        connection.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(payload), task['id']))
        connection.commit()
    assert backend.gpu_idle_reason() is None
    assert scheduler.tick(threaded=False) == []
    assert scheduler.queue_info(task['id'])['state'] == 'waiting_dependency'


def test_b_user_http_submission_during_a_running_batch_gets_next_turn(queued_private_app, monkeypatch):
    a, b, _, scheduler, executed = queued_private_app
    _, abase = prepare(a, ['S01', 'S02', 'S03'])
    _, bbase = prepare(b, ['S01', 'S02'])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def fake_workflow(task_id, payload):
        executed.append((payload['owner_user'], payload['asset_id']))
        if (payload['owner_user'], payload['asset_id']) == ('user001', 'S01'):
            entered.set()
            assert release.wait(5)
        backend.set_task(task_id, status='succeeded')
    monkeypatch.setattr(backend, 'run_workflow_submission', fake_workflow)
    scheduler.completed = lambda task: finished.set()
    response = a.post(abase + '/batches', json={'asset_ids': ['S01', 'S02', 'S03'], 'idempotency_key': 'a-long'})
    assert response.status_code == 200, response.text
    scheduler.tick()
    assert entered.wait(5)
    try:
        response = b.post(bbase + '/batches', json={'asset_ids': ['S01', 'S02'], 'idempotency_key': 'b-midway'})
        assert response.status_code == 200, response.text
        assert all(task['status'] == 'batch_waiting' for task in b.get(bbase + '/tasks').json()['tasks'])
        assert scheduler.tick(threaded=False) == []
    finally:
        release.set()
    assert finished.wait(5)
    for _ in range(4):
        scheduler.tick(threaded=False)
    assert executed == [('user001', 'S01'), ('user002', 'S01'), ('user001', 'S02'), ('user002', 'S02'), ('user001', 'S03')]


def test_uploaded_reference_is_persistently_queued_without_gpu_work(queued_private_app, monkeypatch):
    a, _, _, scheduler, executed = queued_private_app
    _, base = prepare(a, ['S01'])
    def must_not_execute(*args, **kwargs):
        raise AssertionError('Reference upload must not start model work')
    monkeypatch.setattr(backend, 'run_tracked_reference_decomposition', must_not_execute)
    uploaded = a.post(base + '/reverse-analyses', files={'file': ('reference.mp4', b'isolated-video', 'video/mp4')})
    assert uploaded.status_code == 202, uploaded.text
    document = uploaded.json()
    task_id = document['import_task_id']
    task = a.get(base + '/tasks/' + task_id).json()
    assert task['status'] == 'scheduler_waiting'
    assert task['payload']['kind'] == 'reference_decomposition'
    assert task['payload']['owner_user'] == 'user001'
    assert task['queue']['resource'] == 'gpu'
    assert executed == []
    stopped = a.post(base + '/tasks/' + task_id + '/stop')
    assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped'
    assert scheduler.tick(threaded=False) == []


@pytest.mark.parametrize('case', ['reversed', 'self', 'cycle'])
def test_batch_dependency_order_rejects_deadlock_before_writes(queued_private_app, case):
    a, _, _, _, executed = queued_private_app
    project_id, base = prepare(a, ['S01', 'S02'])
    document, path = backend.load_project_plan(project_id)
    segments = {item['id']: item for item in document['segments']}
    request_ids = ['S01', 'S02']
    if case == 'reversed':
        segments['S02']['dependencies'] = ['S01']
        request_ids.reverse()
    elif case == 'self':
        segments['S01']['dependencies'] = ['S01']
    else:
        segments['S01']['dependencies'] = ['S02']
        segments['S02']['dependencies'] = ['S01']
    # Seed an isolated pre-existing plan with its imported dependency metadata.
    path.write_text(json.dumps(document), encoding='utf-8')
    response = a.post(base + '/batches', json={'asset_ids': request_ids, 'idempotency_key': 'invalid-order'})
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'batch_dependency_order'
    assert a.get(base + '/tasks').json()['tasks'] == []
    assert executed == []
