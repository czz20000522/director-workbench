"""Real HTTP/MCP services with isolated accounts, DB, and a fake GPU executor."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from backend import app as backend
from backend import production_presets
from backend.task_scheduler import TaskScheduler
from backend.private_auth import Principal, current_principal
from backend.user_context import UserContext, SessionIdentity
from mcp_server.server import create_server
from test_private_workbench import private_workbench, create
from test_production_presets import preset_client


@pytest.fixture
def isolated_scheduler(monkeypatch, tmp_path):
    busy = {'value': True}
    executions = []
    def queue(path, *args, **kwargs):
        assert path == '/queue', 'An offline acceptance test attempted non-queue ComfyUI access'
        return {'queue_running': [['external-test']] if busy['value'] else [], 'queue_pending': []}
    monkeypatch.setattr(backend, 'request_json', queue)
    monkeypatch.setattr(backend, 'SNAPSHOT_ROOT', tmp_path / 'snapshots')
    def execute(task):
        executions.append(task['id'])
        backend.set_task(task['id'], status='succeeded', result=json.dumps({'test_executor': True}))
    scheduler = TaskScheduler(lambda: backend.db(), backend.DB_LOCK, backend.GPU_ADMISSION_LOCK,
                              backend.scheduler_resource_reason, backend.revalidate_queued_task, execute,
                              global_limit=16, user_limit=8)
    monkeypatch.setattr(backend, 'TASK_SCHEDULER', scheduler)
    return scheduler, busy, executions


def install_private_shot(client, project_id, *, shot_id='S01'):
    root = '/api/projects/' + project_id
    installed = client.post(root + '/production-presets/h3-first-native-square')
    assert installed.status_code == 200, installed.text
    uploaded = client.post(root + '/upload', files=[('files', ('first.png', b'isolated image fixture', 'image/png'))])
    assert uploaded.status_code == 200, uploaded.text
    reference = uploaded.json()['uploaded'][0]
    response = client.post(root + '/plan/segments', json={
        'segment_id': shot_id, 'duration_seconds': 5, 'prompt': 'A character waves', 'first_frame': reference,
    })
    assert response.status_code == 200, response.text
    return root


def test_guide_persists_skip_reopen_conflict_and_accounts_do_not_share(private_workbench, isolated_scheduler):
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    initial = a.get('/api/settings/guide').json()
    assert initial['revision'] == 0 and initial['status'] == 'active'
    updated = a.patch('/api/settings/guide', json={
        'expected_revision': 0, 'project_id': first['id'], 'route': 'reference', 'completed_steps': ['goal', 'project'],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()['revision'] == 1
    assert b.get('/api/settings/guide').json()['completed_steps'] == []
    assert b.get('/api/settings/guide').json()['project_id'] is None
    reopened_client = TestClient(backend.app)
    reopened_client.headers['Authorization'] = a.headers['Authorization']
    assert reopened_client.get('/api/settings/guide').json() == updated.json()
    conflict = a.patch('/api/settings/guide', json={'expected_revision': 0, 'status': 'skipped'})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'settings_revision_conflict'
    skipped = a.patch('/api/settings/guide', json={'expected_revision': 1, 'status': 'skipped'})
    assert skipped.status_code == 200 and skipped.json()['status'] == 'skipped'
    reopened = a.patch('/api/settings/guide', json={'expected_revision': 2, 'status': 'active'})
    assert reopened.status_code == 200
    assert reopened.json()['completed_steps'] == ['goal', 'project']
    unauthorized = b.patch('/api/settings/guide', json={'expected_revision': 0, 'project_id': first['id']})
    assert unauthorized.status_code == 404
    assert b.get('/api/settings/guide').json()['revision'] == 0
    assert a.get('/api/projects/' + second['id'] + '/state').status_code == 404
    assert a.get('/api/projects/' + first['id'] + '/tasks').json()['tasks'] == []


@pytest.mark.parametrize(('preset', 'missing'), [
    ('h3-guided', {'prompt', 'first-frame', 'last-frame', 'guide'}),
    ('h3-first-native-square', {'prompt', 'first-frame'}),
    ('h3-first-native-landscape', {'prompt', 'first-frame'}),
])
def test_state_and_real_preflight_list_all_missing_inputs(preset_client, isolated_scheduler, preset, missing):
    client, _ = preset_client
    root = '/api/projects/preset-test'
    install = client.post(root + '/production-presets/' + preset)
    assert install.status_code == 200, install.text
    assert client.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5}).status_code == 200
    stage_id = production_presets.stage_id(preset)
    state = client.get(root + '/state')
    assert state.status_code == 200, state.text
    row = state.json()['readiness']['segments'][0]
    assert not row['ready']
    assert {item['input_id'] for item in row['blockers']} == missing
    checked = client.post(root + '/pipeline/' + stage_id + '/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 422
    detail = checked.json()['detail']
    assert {item['input_id'] for item in detail['blockers']} == missing
    assert detail['readiness']['blockers'] == row['blockers']
    assert client.get(root + '/tasks').json()['tasks'] == []


def test_non_h3_contract_draft_errors_share_validation_and_do_not_save(preset_client, isolated_scheduler):
    client, project = preset_client
    root = '/api/projects/preset-test'
    workspace = project / 'workspaces/preset-test'
    graph = workspace / 'custom.json'
    graph.write_text(json.dumps({'1': {'class_type': 'TestVideo', 'inputs': {'prompt': '', 'frames': 1}}}), encoding='utf-8')
    manifest = backend.load_project_manifest('preset-test')
    manifest['pipeline'] = [{'id': 'custom-non-h3', 'title': '自定义视频',
        'execution': {'mode': 'comfyui', 'references': [str(graph)]},
        'input_specs': [
            {'id': 'prompt', 'label': '提示词', 'kind': '文本', 'required': True, 'control': 'text', 'binding': {'node_id': '1', 'input': 'prompt'}},
            {'id': 'frames', 'label': '帧数', 'kind': '参数', 'required': True, 'control': 'number', 'min': 1, 'max': 300,
             'binding': {'node_id': '1', 'input': 'frames'}},
        ], 'output_specs': [{'id': 'video', 'kind': '视频'}]}]
    backend.save_project_manifest(manifest)
    assert client.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 2, 'workflow': str(graph)}).status_code == 200
    saved = client.get(root + '/plan').json()
    checked = client.post(root + '/pipeline/custom-non-h3/validate', json={
        'asset_id': 'S01', 'values': {'prompt': '', 'frames': 999, 'undeclared': True},
    })
    assert checked.status_code == 422, checked.text
    detail = checked.json()['detail']
    assert {reason['code'] for reason in detail['blockers']} == {'missing_input', 'invalid_number', 'unknown_input'}
    assert detail['readiness']['source'] == 'draft'
    assert detail['readiness']['revision'] == saved['revision']
    assert client.get(root + '/plan').json() == saved
    state = client.get(root + '/state').json()['readiness']['segments'][0]
    assert state['stage_id'] == 'custom-non-h3' and state['source'] == 'saved'
    assert client.get(root + '/tasks').json()['tasks'] == []


@pytest.mark.parametrize('material_value', [None, True, ['first.png']], ids=['missing-file', 'boolean', 'list'])
def test_missing_prompt_and_unavailable_material_are_reported_together_before_submission(preset_client, isolated_scheduler, material_value):
    client, project = preset_client
    _, _, executions = isolated_scheduler
    root = '/api/projects/preset-test'
    stage_id = production_presets.stage_id('h3-first-native-square')
    assert client.post(root + '/production-presets/h3-first-native-square').status_code == 200
    missing_frame = project / 'workspaces/preset-test/assets/missing.png'
    assert not missing_frame.exists()
    created = client.post(root + '/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'first_frame': str(missing_frame),
    })
    assert created.status_code == 200, created.text
    saved = client.get(root + '/plan').json()
    expected = {('missing_input', 'prompt'), ('material_unavailable', 'first-frame')}
    values = {} if material_value is None else {'first-frame': material_value}

    state = client.get(root + '/state')
    assert state.status_code == 200, state.text
    readiness = state.json()['readiness']['segments'][0]
    assert {(item['code'], item['input_id']) for item in readiness['blockers']} == expected
    assert not readiness['ready'] and not readiness['queueable']
    assert 'submit' not in {item['id'] for item in readiness['allowed_actions']}
    preflight = client.post(root + '/pipeline/' + stage_id + '/validate', json={'asset_id': 'S01', 'values': values})
    assert preflight.status_code == 422, preflight.text
    detail = preflight.json()['detail']
    assert {(item['code'], item['input_id']) for item in detail['blockers']} == expected
    assert {(item['code'], item['input_id']) for item in detail['readiness']['blockers']} == expected
    assert not detail['readiness']['ready'] and not detail['readiness']['queueable']
    if material_value is None:
        assert detail['readiness']['blockers'] == readiness['blockers']
    submitted = client.post(root + '/tasks', json={
        'asset_id': 'S01', 'pipeline_stage_id': stage_id, 'idempotency_key': 'invalid-material-and-prompt',
        'pipeline_values': values,
    })
    assert submitted.status_code == 422, submitted.text
    assert {(item['code'], item['input_id']) for item in submitted.json()['detail']['blockers']} == expected
    assert client.get(root + '/plan').json() == saved
    assert client.get(root + '/tasks').json()['tasks'] == []
    assert executions == []


def test_missing_assembly_builder_blocks_public_preflight_state_and_submission(preset_client, isolated_scheduler, monkeypatch):
    client, project = preset_client
    _, _, executions = isolated_scheduler
    root = '/api/projects/preset-test'
    assert client.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5}).status_code == 200
    # A synthetic completed candidate isolates builder availability from video/join readiness.
    candidate = project / 'workspaces/preset-test/candidate.mp4'
    candidate.write_bytes(b'isolated completed candidate')
    document, plan_path = backend.load_project_plan('preset-test')
    document['segments'][0]['video'] = {'path': str(candidate)}
    document['segments'][0]['status'] = 'video_generated'
    backend.write_plan_version(plan_path, document)
    manifest = backend.load_project_manifest('preset-test')
    manifest['assembly']['builder'] = 'tools/missing-assembly-builder.py'
    backend.save_project_manifest(manifest)
    monkeypatch.setattr(backend, 'build_assembly_snapshot',
                        lambda *args, **kwargs: pytest.fail('Missing builders must fail before snapshot creation'))
    saved = client.get(root + '/plan').json()

    preflight = client.get(root + '/assembly/preflight')
    assert preflight.status_code == 200, preflight.text
    assert not preflight.json()['ready'] and preflight.json()['missing_videos'] == []
    assert [item['code'] for item in preflight.json()['blockers']] == ['assembly_builder_missing']
    state = client.get(root + '/state')
    assert state.status_code == 200, state.text
    readiness = state.json()['readiness']['assembly']
    assert not readiness['ready']
    assert [item['code'] for item in readiness['blockers']] == ['assembly_builder_missing']
    assert 'assemble' not in {item['id'] for item in readiness['allowed_actions']}
    submitted = client.post(root + '/assembly', json={
        'expected_revision': saved['revision'], 'idempotency_key': 'missing-builder',
    })
    assert submitted.status_code == 422, submitted.text
    assert submitted.json()['detail']['code'] == 'assembly_builder_missing'
    assert [item['code'] for item in submitted.json()['detail']['blockers']] == ['assembly_builder_missing']
    assert client.get(root + '/plan').json() == saved
    assert client.get(root + '/tasks').json()['tasks'] == []
    assert candidate.read_bytes() == b'isolated completed candidate'
    assert executions == []


@pytest.mark.parametrize('duration', [3.9, 15.1])
def test_h3_invalid_draft_lists_multiple_inputs_without_changing_saved_plan(preset_client, isolated_scheduler, duration):
    client, _ = preset_client
    root = '/api/projects/preset-test'
    assert client.post(root + '/production-presets/h3-first-native-square').status_code == 200
    assert client.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5}).status_code == 200
    saved = client.get(root + '/plan').json()
    response = client.post(root + '/pipeline/' + production_presets.stage_id('h3-first-native-square') + '/validate',
                           json={'asset_id': 'S01', 'values': {'duration': duration, 'prompt': '', 'first-frame': ''}})
    assert response.status_code == 422
    detail = response.json()['detail']
    assert {item['input_id'] for item in detail['blockers']} == {'duration', 'prompt', 'first-frame'}
    assert detail['readiness']['source'] == 'draft' and not detail['readiness']['queueable']
    assert client.get(root + '/plan').json() == saved
    assert client.get(root + '/tasks').json()['tasks'] == []


def test_busy_queue_receipt_limits_idempotency_cancel_and_user_isolation(private_workbench, isolated_scheduler):
    a, b, _, _ = private_workbench
    scheduler, busy, executions = isolated_scheduler
    first, second = create(a), create(b)
    root_a = install_private_shot(a, first['id'])
    root_b = install_private_shot(b, second['id'])
    first_request = {'asset_id': 'S01', 'idempotency_key': 'same-per-user-key'}
    submitted_a = a.post(root_a + '/tasks', json=first_request)
    submitted_b = b.post(root_b + '/tasks', json=first_request)
    assert submitted_a.status_code == submitted_b.status_code == 200, (submitted_a.text, submitted_b.text)
    task_a, task_b = submitted_a.json(), submitted_b.json()
    assert task_a['id'] != task_b['id']
    assert task_a['status'] == task_b['status'] == 'scheduler_waiting'
    assert scheduler.tick(threaded=False) == [] and executions == []
    queued = a.get('/api/queue')
    assert queued.status_code == 200, queued.text
    assert [task['id'] for task in queued.json()['tasks']] == [task_a['id']]
    assert queued.json()['tasks'][0]['queue']['reason']['code'] == 'external_queue_busy'
    assert a.post(root_a + '/tasks', json=first_request).json()['id'] == task_a['id']
    conflict = a.post(root_a + '/tasks', json={**first_request, 'prompt': 'different frozen prompt'})
    assert conflict.status_code == 409
    assert b.get(root_a + '/tasks/' + task_a['id']).status_code == 404
    assert b.post(root_b + '/tasks/' + task_a['id'] + '/stop').status_code == 404
    # Limits apply atomically to new frozen admissions; retries keep old receipts.
    scheduler.user_limit = 1
    assert a.post(root_a + '/plan/segments', json={'segment_id': 'S02', 'duration_seconds': 5,
        'prompt': 'second shot', 'first_frame': a.get(root_a + '/plan').json()['segments'][0]['keyframes']['first']}).status_code == 200
    over_limit = a.post(root_a + '/tasks', json={'asset_id': 'S02', 'idempotency_key': 'limit-key'})
    assert over_limit.status_code == 429 and over_limit.json()['detail']['code'] == 'queue_user_limit'
    cancelled = b.post(root_b + '/tasks/' + task_b['id'] + '/stop')
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'stopped'
    busy['value'] = False
    assert scheduler.tick(threaded=False) == [task_a['id']]
    assert executions == [task_a['id']]
    assert a.get(root_a + '/tasks/' + task_a['id']).json()['status'] == 'succeeded'


def test_global_queue_quota_is_atomic_and_original_receipts_survive_limit(private_workbench, isolated_scheduler):
    a, b, _, _ = private_workbench
    scheduler, _, _ = isolated_scheduler
    root_a = install_private_shot(a, create(a)['id'])
    root_b = install_private_shot(b, create(b)['id'])
    scheduler.global_limit = 1
    request = {'asset_id': 'S01', 'idempotency_key': 'only-slot'}
    first = a.post(root_a + '/tasks', json=request)
    assert first.status_code == 200
    limited = b.post(root_b + '/tasks', json=request)
    assert limited.status_code == 429 and limited.json()['detail']['code'] == 'queue_global_limit'
    assert a.post(root_a + '/tasks', json=request).json()['id'] == first.json()['id']
    assert b.get(root_b + '/tasks').json()['tasks'] == []


def test_http_batch_and_mcp_middle_arrival_rotate_at_task_boundary(private_workbench, isolated_scheduler):
    a, b, _, _ = private_workbench
    scheduler, busy, executions = isolated_scheduler
    first, second = create(a), create(b)
    root_a = install_private_shot(a, first['id'])
    root_b = install_private_shot(b, second['id'])
    first_frame = a.get(root_a + '/plan').json()['segments'][0]['keyframes']['first']
    for shot_id in ('S02', 'S03'):
        assert a.post(root_a + '/plan/segments', json={
            'segment_id': shot_id, 'duration_seconds': 5, 'prompt': shot_id, 'first_frame': first_frame,
        }).status_code == 200
    request = {'asset_ids': ['S01', 'S02', 'S03'], 'idempotency_key': 'long-batch'}
    batch = a.post(root_a + '/batches', json=request)
    assert batch.status_code == 200, batch.text
    ids = batch.json()['task_ids']
    assert a.post(root_a + '/batches', json=request).json()['task_ids'] == ids
    assert scheduler.tick(threaded=False) == []
    busy['value'] = False
    assert scheduler.tick(threaded=False) == [ids[0]]
    async def join():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app), session_token=b.headers['Authorization'][7:])
        async with Client(server) as agent:
            result = (await agent.call_tool('submit_shot', {
                'project_id': second['id'], 'asset_id': 'S01', 'idempotency_key': 'middle-arrival',
            })).structured_content
            assert result['ok'], result
            return result['data']['id']
    middle = asyncio.run(join())
    assert scheduler.tick(threaded=False) == [middle]
    assert scheduler.tick(threaded=False) == [ids[1]]
    assert scheduler.tick(threaded=False) == [ids[2]]
    assert executions == [ids[0], middle, ids[1], ids[2]]
    assert b.get(root_b + '/tasks').json()['tasks'][0]['status'] == 'succeeded'


def test_real_mcp_reads_and_writes_same_readiness_guide_and_queue(private_workbench, isolated_scheduler):
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    root = install_private_shot(a, first['id'])
    a.patch('/api/settings/guide', json={'expected_revision': 0, 'project_id': first['id'], 'route': 'idea'})
    http_state = a.get(root + '/state').json()
    http_guide = a.get('/api/settings/guide').json()
    http_queue = a.get('/api/queue').json()
    invalid_values = {'prompt': '', 'first-frame': '', 'duration': 3.9}
    stage_id = production_presets.stage_id('h3-first-native-square')
    http_preflight = a.post(root + '/pipeline/' + stage_id + '/validate', json={
        'asset_id': 'S01', 'values': invalid_values,
    })
    assert http_preflight.status_code == 422
    async def verify():
        transport = httpx.ASGITransport(app=backend.app)
        server = create_server('http://localhost', transport, session_token=a.headers['Authorization'][7:])
        async with Client(server) as agent:
            state = (await agent.call_tool('read_project', {'project_id': first['id'], 'section': 'state'})).structured_content
            assert state['ok'] and state['data'] == http_state
            guide = (await agent.call_tool('read_guide_settings', {})).structured_content
            assert guide['ok'] and guide['data'] == http_guide
            queue = (await agent.call_tool('read_queue', {})).structured_content
            assert queue['ok'] and queue['data'] == http_queue
            invalid = (await agent.call_tool('preflight', {
                'project_id': first['id'], 'stage_id': stage_id, 'asset_id': 'S01', 'values': invalid_values,
            })).structured_content
            assert invalid['error']['http_status'] == 422
            assert invalid['error']['detail'] == http_preflight.json()['detail']
            changed = (await agent.call_tool('update_guide_settings', {
                'expected_revision': http_guide['revision'], 'changes': {'status': 'skipped'},
            })).structured_content
            assert changed['ok'] and changed['data']['status'] == 'skipped'
            denied = (await agent.call_tool('read_project', {'project_id': second['id'], 'section': 'state'})).structured_content
            assert denied['error']['http_status'] == 404
    asyncio.run(verify())
    assert a.get('/api/settings/guide').json()['status'] == 'skipped'
    assert b.get('/api/settings/guide').json()['status'] == 'active'
    assert a.get(root + '/tasks').json()['tasks'] == []


@pytest.mark.parametrize(('has_candidate', 'review_status', 'expected_state', 'completed'), [
    (False, None, 'blocked', False),
    (True, None, 'pending_review', False),
    (True, 'pending_review', 'pending_review', False),
    (True, 'approved', 'adopted', True),
    (True, 'changes_requested', 'redo_required', False),
    (True, 'stale', 'review_stale', False),
])
def test_guide_assembly_completion_requires_current_review_http_and_mcp(
        private_workbench, isolated_scheduler, has_candidate, review_status, expected_state, completed):
    a, b, ownership, _ = private_workbench
    first = create(a)
    root = '/api/projects/' + first['id']
    # Synthetic existing candidate, only in the fixture's private workspace.
    workspace = UserContext('user001', ownership.layout).project(first['id'])
    plan_path = workspace / 'plans/plan.json'
    document = a.get(root + '/plan').json()
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = workspace / 'candidate.mp4'
    if has_candidate:
        candidate.write_bytes(b'isolated candidate fixture')
        document['assembly'] = {'output': str(candidate), 'status': 'assembled'}
    backend.write_plan_version(plan_path, document)
    if review_status is not None:
        review = a.post(root + '/reviews', json={
            'asset_id': 'MASTER', 'stage': 'final', 'status': review_status,
            'note': 'Explicit fixture review', 'expected_revision': 0,
        })
        assert review.status_code == 200, review.text
    # Learning progress must never approve an existing candidate.
    settings = a.patch('/api/settings/guide', json={
        'expected_revision': 0, 'project_id': first['id'], 'completed_steps': ['assembly'],
    })
    assert settings.status_code == 200, settings.text
    original = a.get(root + '/state').json()
    original_plan = a.get(root + '/plan').json()
    response = a.get('/api/settings/guide', params={'project_id': first['id']})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['readiness'] == original['readiness']
    assert data['readiness']['assembly']['state'] == expected_state
    step = next(item for item in data['steps'] if item['id'] == 'assembly')
    assert step['completed'] is completed
    assert step['state'] == expected_state
    assert step['state_label']
    assert step['title'] == '装配并通过成片审核'
    export_step = next(item for item in data['steps'] if item['id'] == 'export')
    assert export_step['completed'] is False
    assert export_step['available'] is completed
    if completed:
        assert '可以导出' in export_step['state_label']
    assert b.get('/api/settings/guide', params={'project_id': first['id']}).status_code == 404

    async def verify():
        transport = httpx.ASGITransport(app=backend.app)
        server = create_server('http://localhost', transport, session_token=a.headers['Authorization'][7:])
        async with Client(server) as agent:
            result = (await agent.call_tool('read_guide_settings', {'project_id': first['id']})).structured_content
            assert result['ok'] and result['data'] == data
    asyncio.run(verify())
    assert a.get(root + '/state').json() == original
    assert a.get(root + '/plan').json() == original_plan
    assert a.get(root + '/tasks').json()['tasks'] == []
    if has_candidate:
        assert candidate.read_bytes() == b'isolated candidate fixture'
    else:
        assert not candidate.exists()


def test_guide_new_assembly_version_uses_existing_review_invalidation(
        private_workbench, isolated_scheduler, monkeypatch):
    a, _, ownership, _ = private_workbench
    first = create(a)
    root = '/api/projects/' + first['id']
    workspace = UserContext('user001', ownership.layout).project(first['id'])
    plan_path = workspace / 'plans/plan.json'
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    old = workspace / 'old.mp4'
    new = workspace / 'new.mp4'
    old.write_bytes(b'old fixture candidate')
    new.write_bytes(b'new fixture candidate')
    document = a.get(root + '/plan').json()
    document['assembly'] = {'output': str(old), 'prompt_id': 'old', 'status': 'candidate_generated'}
    backend.write_plan_version(plan_path, document)
    approved = a.post(root + '/reviews', json={
        'asset_id': 'MASTER', 'stage': 'final', 'status': 'approved',
        'candidate_ref': str(old), 'expected_revision': 0,
    })
    assert approved.status_code == 200, approved.text
    before = a.get('/api/settings/guide', params={'project_id': first['id']}).json()
    assert next(row for row in before['steps'] if row['id'] == 'assembly')['completed']
    monkeypatch.setattr(backend, 'assembly_duration', lambda _: 5.0)
    # The real execution receipt handler invalidates the old final review.
    # No task or executor is started; this is a synthetic offline receipt.
    context = current_principal.set(Principal(
        SessionIdentity('user001', time.time() + 60), a.headers['Authorization'][7:], False))
    try:
        backend.record_assembly_result('new', 'fixture.json', {
            'outputs': ['new.mp4'], 'published_outputs': [str(new)],
        }, plan_path)
    finally:
        current_principal.reset(context)
    state = a.get(root + '/state').json()
    original_plan = a.get(root + '/plan').json()
    result = a.get('/api/settings/guide', params={'project_id': first['id']}).json()
    assert result['readiness'] == state['readiness']
    assert state['reviews']['MASTER']['final']['status'] == 'stale'
    assert state['reviews']['MASTER']['final']['candidate_ref'] == str(old)
    assert len(state['history']) == 2
    step = next(row for row in result['steps'] if row['id'] == 'assembly')
    assert step['state'] == 'review_stale' and not step['completed']
    assert not next(row for row in result['steps'] if row['id'] == 'export')['available']
    async def verify():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app),
                               session_token=a.headers['Authorization'][7:])
        async with Client(server) as agent:
            guide = (await agent.call_tool('read_guide_settings', {'project_id': first['id']})).structured_content
            assert guide['ok'] and guide['data'] == result
    asyncio.run(verify())
    assert a.get(root + '/state').json() == state
    assert a.get(root + '/plan').json() == original_plan
    assert old.read_bytes() == b'old fixture candidate'
    assert a.get(root + '/tasks').json()['tasks'] == []
