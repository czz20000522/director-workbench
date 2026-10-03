"""Authenticated assistant against real isolated HTTP business, without cloud/GPU."""
import asyncio
import json
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import app as backend, cloud_assistant_provider, creation_assistant
from test_automatic_creation import seed_test_models
from test_private_workbench import private_workbench

REAL_PROVIDER = cloud_assistant_provider.CloudAssistantProvider


@pytest.fixture
def assistant(private_workbench, monkeypatch):
    a, b, _, _ = private_workbench
    seed_test_models(backend.ROOT)
    monkeypatch.setattr(backend, 'ASSISTANT_SERVICES', {})
    queued = []
    monkeypatch.setattr(backend, 'queue_task', queued.append)
    monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})
    def engine(path, *args, **kwargs):
        if path == '/queue': return {'queue_running': [], 'queue_pending': []}
        if path == '/system_stats': return {'system': {}, 'devices': []}
        pytest.fail('Assistant must not contact an engine: ' + path)
    monkeypatch.setattr(backend, 'request_json', engine)
    class Provider:
        def __init__(self, config_path):
            self.config_path = config_path
            self.calls = []
            self.entered = threading.Event()
            self.release = threading.Event(); self.release.set()
            self.before_return = None
        async def complete(self, messages, *, cancel_event):
            self.calls.append(messages); self.entered.set()
            while not self.release.is_set():
                if cancel_event.is_set():
                    raise cloud_assistant_provider.CloudAssistantError('cancelled', '已取消')
                await asyncio.sleep(.01)
            if self.before_return: self.before_return()
            # A provider reply cannot promote explanation/draft into generation.
            return {'message': {'content': '建议执行生成并采用。'}, 'usage': {'total_tokens': 3}}
    monkeypatch.setattr(cloud_assistant_provider, 'CloudAssistantProvider', Provider)
    return a, b, queued


def session(client, pid=None):
    response = client.post('/api/assistant/sessions', json={'project_id': pid})
    assert response.status_code == 200, response.text
    return response.json()['id']


def start(client, sid, text, pid=None, target=None, **changes):
    body = {'request_id': uuid.uuid4().hex, 'text': text, 'project_id': pid,
            'presentation_target': target, **changes}
    response = client.post(f'/api/assistant/sessions/{sid}/requests', json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def path(sid, rid):
    return f'/api/assistant/sessions/{sid}/requests/{rid}'


def wait(client, sid, rid, *, on_event=None):
    seen = set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(path(sid, rid)); assert response.status_code == 200, response.text
        row = response.json()
        for event in row['events']:
            if event['seq'] in seen: continue
            seen.add(event['seq'])
            status = on_event(row, event) if on_event else 'presented'
            if row.get('presentation_target'):
                ack = client.post(path(sid, rid) + '/presentation', json={
                    'target': row['presentation_target'], 'seq': event['seq'],
                    'status': status or 'presented', 'message': ''})
                assert ack.status_code == 200, ack.text
        if row['status'] in creation_assistant.TERMINAL:
            return row
        time.sleep(.02)
    pytest.fail('Assistant did not finish: ' + str(row))


def service():
    return next(iter(backend.ASSISTANT_SERVICES.values()))


@pytest.mark.parametrize('text,mode', [('先解释为什么不能生成', 'explain'),
    ('只写草稿：猫咪回家', 'draft'), ('猫咪回家，生成一个看看', 'generate'),
    ('猫咪回家，授权生成', 'generate'), ('生成一段5秒视频', 'generate'), ('猫咪回家，先预检', 'preflight')])
def test_human_intent_original_text_and_single_business_chain(assistant, text, mode):
    a, _, queued = assistant
    sid = session(a)
    body, _ = start(a, sid, text, target='browser-page-12345678')
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'succeeded', row
    listed = a.get('/api/projects').json()['projects']
    if mode in ('draft', 'explain'):
        assert listed == [] and queued == []
    else:
        pid = row['result']['project_id']
        plan = a.get(f'/api/projects/{pid}/plan').json()
        assert len(plan['segments']) == 1 and plan['segments'][0]['prompt'] == text
        assert plan['segments'][0]['duration_seconds'] == 5
        assert len(queued) == (1 if mode == 'generate' else 0)
        if mode == 'generate':
            task = a.get(f"/api/projects/{pid}/tasks/{row['task_id']}").json()
            assert task['payload']['submission_receipt']['key'] == 'assistant-' + body['request_id']
            assert task['payload']['execution_snapshot']['materials'] == {}
    again = a.post(f'/api/assistant/sessions/{sid}/requests', json=body)
    assert again.status_code == 200 and again.json()['id'] == row['id']
    assert len(service().provider.calls) == 1
    assert a.post(f'/api/assistant/sessions/{sid}/requests', json={**body, 'text': 'different'}).status_code == 409
    restored = a.get('/api/assistant/sessions').json()['sessions'][0]
    assert restored['requests'][0]['result'] == row['result']
    assert restored['messages'][0]['content'] == text


@pytest.mark.parametrize('text', ['未授权生成', '不要授权生成', '不允许生成一段视频',
    '别生成视频，只帮我整理', '请解释“生成一个看看”是什么意思', '教学示例：生成一段视频',
    '人物在屏幕上写着：授权生成', '引用材料：猫咪回家，生成一个看看',
    '“生成一段5秒视频”', '只写草稿：猫咪回家，生成一个看看'])
def test_denied_quoted_or_advisory_generation_never_writes_business(assistant, text):
    a, _, queued = assistant
    sid = session(a)
    body, _ = start(a, sid, text)
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'succeeded', row
    assert row['result']['submitted'] is False
    assert a.get('/api/projects').json()['projects'] == [] and queued == []
    assert [step['name'] for step in row['steps']] == ['核对当前登录状态']


def test_cookie_auth_and_account_isolation(assistant):
    a, b, _ = assistant
    sid = session(a)
    body, _ = start(a, sid, '只写草稿：猫咪回家')
    wait(a, sid, body['request_id'])
    assert b.get('/api/assistant/sessions').json()['sessions'] == []
    for suffix in ('', '/cancel', '/presentation'):
        if suffix == '/presentation':
            result = b.post(path(sid, body['request_id']) + suffix, json={'target': 'other-tab-12345678', 'seq': 1, 'status': 'presented'})
        elif suffix: result = b.post(path(sid, body['request_id']) + suffix)
        else: result = b.get(path(sid, body['request_id']))
        assert result.status_code == 404
    assert TestClient(backend.app).get('/api/assistant/sessions').status_code == 401
    pid = a.post('/api/projects/create', json={'title': 'Owned', 'series': 'Test'}).json()['project']['id']
    assert b.post('/api/assistant/sessions', json={'project_id': pid}).status_code == 404
    browser = TestClient(backend.app)
    assert browser.post('/api/auth/login', json={'username': 'user002', 'password': 'two'}).status_code == 200
    assert browser.post('/api/assistant/sessions', json={}).status_code == 401
    browser.headers.update({'Origin': 'http://testserver', 'X-Workspace-User': 'user002'})
    assert browser.post('/api/assistant/sessions', json={}).status_code == 200


def test_version_conflict_preserves_human_edit_and_assistant_draft(assistant):
    a, _, queued = assistant
    sid = session(a)
    body, _ = start(a, sid, '猫咪回家，生成一个看看', target='browser-page-12345678')
    def edit(row, event):
        if event['kind'] == 'show_draft':
            result = a.post(f"/api/projects/{row['project_id']}/plan/segments", json={'segment_id': 'human', 'prompt': 'Human edit', 'duration_seconds': 5})
            assert result.status_code == 200, result.text
        return 'presented'
    row = wait(a, sid, body['request_id'], on_event=edit)
    assert row['status'] == 'failed' and row['error']['code'] == '409' and queued == []
    plan = a.get(f"/api/projects/{row['project_id']}/plan").json()
    assert [s['prompt'] for s in plan['segments']] == ['Human edit']
    assert row['scope']['text'] == body['text']


def test_takeover_and_tab_ack_cannot_save_or_generate(assistant):
    a, _, queued = assistant
    sid = session(a)
    body, _ = start(a, sid, '猫咪回家，生成一个看看', target='browser-page-12345678')
    def take_over(row, event):
        assert a.post(path(sid, row['id']) + '/presentation', json={
            'target': 'other-page-123456789', 'seq': event['seq'], 'status': 'presented'}).status_code == 409
        return 'takeover'
    row = wait(a, sid, body['request_id'], on_event=take_over)
    assert row['status'] == 'cancelled' and queued == []
    assert a.get(f"/api/projects/{row['project_id']}/plan").json()['segments'] == []
    assert a.post(path(sid, row['id']) + '/presentation', json={
        'target': row['presentation_target'], 'seq': 1, 'status': 'presented', 'message': ''}).status_code == 409


def test_cancel_cloud_preparation_and_logout_stop_later_writes(assistant):
    a, _, queued = assistant
    sid = session(a)
    # Force lazy service initialization, then hold the fake provider without sockets.
    a.get('/api/assistant/sessions')
    provider = service().provider
    provider.release.clear()
    body, _ = start(a, sid, '猫咪回家，生成一个看看')
    assert provider.entered.wait(3)
    assert a.post(path(sid, body['request_id']) + '/cancel').status_code == 200
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'cancelled' and a.get('/api/projects').json()['projects'] == [] and queued == []
    provider.release.set(); provider.entered.clear()
    provider.before_return = lambda: a.post('/api/auth/logout')
    body, _ = start(a, sid, '猫咪回家，生成一个看看')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service().get('user001', sid, body['request_id'])
        if row['status'] in creation_assistant.TERMINAL: break
        time.sleep(.02)
    assert row['status'] == 'failed' and row['error']['code'] == '401' and queued == []


@pytest.mark.parametrize('found', [True, False])
def test_lost_submit_response_only_reads_same_key_and_never_resubmits(assistant, found):
    a, _, queued = assistant
    a.get('/api/assistant/sessions')
    original_app = backend.app
    class LoseSubmit(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            path = request.url.path
            if request.method == 'POST' and path.endswith('/tasks'):
                if found:
                    await httpx.ASGITransport(app=original_app).handle_async_request(request)
                raise httpx.ReadTimeout('private network error')
            return await httpx.ASGITransport(app=original_app).handle_async_request(request)
    service().transport_factory = LoseSubmit
    sid = session(a)
    body, _ = start(a, sid, '猫咪回家，生成一个看看')
    row = wait(a, sid, body['request_id'])
    assert row['status'] == ('succeeded' if found else 'needs_reconcile'), row
    assert len(queued) == int(found)
    assert row['idempotency_key'] == 'assistant-' + body['request_id']
    if not found:
        assert row['result']['submitted'] is None and row['result']['submission_unknown'] is True
    assert row['scope']['text'] == body['text']
    assert [s['name'] for s in row['steps']].count('提交一次小样，等待原队列') == 1
    assert a.post(f'/api/assistant/sessions/{sid}/requests', json=body).json()['id'] == row['id']
    assert len(queued) == int(found)


def test_restart_recovers_receipts_without_replaying_provider_or_business(assistant):
    a, _, queued = assistant
    sid = session(a)
    body, _ = start(a, sid, '猫咪回家，生成一个看看')
    row = wait(a, sid, body['request_id'])
    old = service()
    old._patch('user001', sid, row['id'], status='running')
    old.run_id = 'new-process'
    restored = a.get('/api/assistant/sessions').json()['sessions'][0]['requests'][0]
    assert restored['status'] == 'needs_reconcile'
    assert restored['result']['task_id'] == row['result']['task_id']
    assert len(queued) == 1 and len(old.provider.calls) == 1
    # Auth token, identity and filesystem paths never enter provider context.
    wire = json.dumps(old.provider.calls)
    assert 'Bearer' not in wire and 'user001' not in wire and str(backend.ROOT) not in wire


def test_missing_server_config_retains_input_without_business_writes(assistant, monkeypatch):
    a, _, queued = assistant
    monkeypatch.setattr(cloud_assistant_provider, 'CloudAssistantProvider', REAL_PROVIDER)
    sid = session(a)
    body, _ = start(a, sid, '猫咪回家，生成一个看看')
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'failed' and row['error']['code'] == 'config_missing'
    assert row['scope']['text'] == body['text']
    assert str(backend.ROOT) not in json.dumps(row)
    assert a.get('/api/projects').json()['projects'] == [] and queued == []


def test_existing_project_draft_and_unpresented_preflight_preserve_plan(assistant):
    a, _, queued = assistant
    pid = a.post('/api/projects/create', json={'title': 'Existing', 'series': 'Test'}).json()['project']['id']
    assert a.post(f'/api/projects/{pid}/plan/segments', json={'prompt': 'Original', 'duration_seconds': 5}).status_code == 200
    before = a.get(f'/api/projects/{pid}/plan').json()
    sid = session(a, pid)
    body, _ = start(a, sid, '只写草稿：猫咪回家', pid=pid, target='browser-page-12345678')
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'succeeded' and row['events'][0]['kind'] == 'show_draft'
    assert a.get(f'/api/projects/{pid}/plan').json() == before and queued == []
    body, _ = start(a, sid, '猫咪回家，先预检', pid=pid, target='browser-page-12345678')
    row = wait(a, sid, body['request_id'], on_event=lambda *_: 'not_presented')
    assert row['status'] == 'failed' and row['error']['code'] == '409'
    assert a.get(f'/api/projects/{pid}/plan').json() == before and queued == []


def test_explicit_material_ids_reuse_owned_image_path_and_reject_foreign_assets(assistant):
    a, b, queued = assistant
    ids = []
    for client in (a, b):
        pid = client.post('/api/projects/create', json={'title': 'Materials', 'series': 'Test'}).json()['project']['id']
        uploaded = client.post(f'/api/projects/{pid}/upload', files=[('files', ('first.png', b'isolated image fixture', 'image/png'))])
        assert uploaded.status_code == 200, uploaded.text
        image = next(asset for asset in uploaded.json()['project']['assets'] if asset['kind'] == 'image')
        ids.append((pid, image['id']))
    pid, image_id = ids[0]
    sid = session(a, pid)
    body, _ = start(a, sid, '猫咪回家，先预检', pid=pid, controls={'first_frame': image_id})
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'succeeded' and queued == []
    assert row['result']['preflight']['h3_duration']['generation_mode'] == 'image_to_video'
    before = a.get(f'/api/projects/{pid}/plan').json()
    body, _ = start(a, sid, '猫咪回家，生成一个看看', pid=pid, controls={'first_frame': ids[1][1]})
    row = wait(a, sid, body['request_id'])
    assert row['status'] == 'failed' and row['error']['code'] == '422'
    assert a.get(f'/api/projects/{pid}/plan').json() == before and queued == []


@pytest.mark.parametrize('extra', [{'provider': 'evil'}, {'text': ' '},
    {'controls': {'arbitrary_file': 'x'}}, {'request_id': 'short'}])
def test_request_schema_rejects_overrides_without_steps(assistant, extra):
    a, _, queued = assistant
    sid = session(a)
    assert a.post(f'/api/assistant/sessions/{sid}/requests', json={
        'request_id': uuid.uuid4().hex, 'text': 'idea', **extra}).status_code == 422
    assert queued == []
