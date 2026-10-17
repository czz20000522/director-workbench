import json
import asyncio
import io
import time
import wave
from pathlib import Path
import uuid
import subprocess
from fastapi import HTTPException

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import app as backend
from backend.private_mode import install
from backend.private_auth import Principal, current_principal
from backend.private_sessions import AccountStore
from backend.user_context import Layout, SessionIdentity, UserContext


def test_supported_private_media_extensions_create_asset_records():
    for kind in ('image', 'audio', 'video'):
        for suffix in backend.MATERIAL_EXTENSIONS[kind]:
            alias = f'uuid{suffix}'
            record = backend.imported_asset_records({alias: f'D:/assets/{alias}'}, {alias: f'原素材{suffix}'})
            assert len(record) == 1
            assert record[0]['kind'] == kind
            assert record[0]['title'] == f'原素材{suffix}'


@pytest.fixture
def private_workbench(tmp_path, monkeypatch):
    project = tmp_path / 'ComfyUI-Workspace' / 'project'
    catalog = project / 'projects'
    catalog.mkdir(parents=True)
    (catalog / 'catalog.json').write_text('{"projects": []}', encoding='utf-8')
    root = tmp_path / 'ComfyUI-Workspace' / 'private-workspaces'
    for name, value in {'ROOT': tmp_path, 'PROJECT': project, 'PROJECTS_ROOT': catalog,
                        'PROJECT_CATALOG_PATH': catalog / 'catalog.json', 'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
                        'DB_PATH': tmp_path / 'tasks.sqlite3', 'RUNTIME': tmp_path / 'runtime', 'INPUT_ROOT': tmp_path / 'input',
                        'OUTPUT_ROOT': tmp_path / 'output', 'PRIVATE_WORKSPACES': None}.items():
        monkeypatch.setattr(backend, name, value)
    app = FastAPI()
    app.router.routes = list(backend.app.router.routes)
    monkeypatch.setattr(backend, 'app', app)
    store = AccountStore(tmp_path / 'accounts.json', permitted_root=tmp_path)
    store.create_account('user001', 'one')
    store.create_account('user002', 'two')
    store.create_account('user000', 'admin')
    sessions, ownership = install(backend, accounts=store,
        layout=Layout(root, permitted_roots=(tmp_path,)), origins=('http://testserver',), administrators=('user000',))
    a, b = TestClient(app), TestClient(app)
    a.headers['Authorization'] = 'Bearer ' + sessions.login('user001', 'one')
    b.headers['Authorization'] = 'Bearer ' + sessions.login('user002', 'two')
    return a, b, ownership, project


def create(client):
    response = client.post('/api/projects/create', json={'title': '同名作品', 'series': '同系列', 'project_id': 'same', 'select': True, 'creation_mode': 'advanced'})
    assert response.status_code == 200, response.text
    return response.json()['project']


def test_same_name_project_identity_selection_and_listing(private_workbench):
    a, b, ownership, _ = private_workbench
    first, second = create(a), create(b)
    assert first['id'] != second['id']
    for client, own, other in ((a, first, second), (b, second, first)):
        listing = client.get('/api/projects').json()
        assert [p['id'] for p in listing['projects']] == [own['id']]
        assert client.get('/api/project').json()['id'] == own['id']
        assert client.get('/api/projects/' + other['id']).status_code == 404
        assert client.post('/api/projects/select', json={'project_id': other['id']}).status_code == 404
    assert ownership.read()['projects'][first['id']] == 'user001'
    assert backend.CURRENT_PROJECT_ID not in {first['id'], second['id']}


def test_physical_series_visible_and_isolated(private_workbench):
    a, b, ownership, _ = private_workbench
    physical = UserContext('user001', ownership.layout).path('series/东方美学/月下玉兰')
    physical.mkdir(parents=True)
    (physical / 'sample.png').write_bytes(b'picture')
    assert a.get('/api/private/series').json()['series'] == [
        {'name': '东方美学', 'works': [{'name': '月下玉兰', 'project_id': None}]}
    ]
    assert b.get('/api/private/series').json()['series'] == []
    media = a.get('/api/private/series/东方美学/works/月下玉兰/media')
    assert media.status_code == 200
    assert media.json()['files'][0]['relative_path'] == 'sample.png'
    assert b.get('/api/private/series/东方美学/works/月下玉兰/media').status_code == 404
    created = a.post('/api/private/series', json={'name': '新系列'})
    assert created.status_code == 201
    assert a.post('/api/private/series', json={'name': '新系列'}).status_code == 409
    assert a.post('/api/private/series', json={'name': '../other'}).status_code == 422
    assert {item['name'] for item in a.get('/api/private/series').json()['series']} == {'东方美学', '新系列'}


def test_delete_preview_and_apply_stay_within_owner(private_workbench):
    a, b, ownership, _ = private_workbench
    item = create(a)
    folder = UserContext('user001', ownership.layout).project(item['id'])
    (folder / 'assets/image/first.png').write_bytes(b'old-artifact')
    series = folder.parent.name
    work = folder.name
    path = f'/api/private/series/{series}/works/{work}'
    preview = a.post(path + '/deletion-plan')
    assert preview.status_code == 200, preview.text
    assert preview.json()['snapshot']['files'] >= 2
    assert b.post(path + '/deletion-plan').status_code == 404
    assert a.request('DELETE', path, json={'plan_id': preview.json()['plan_id'], 'confirm': 'wrong'}).status_code == 422
    preview = a.post(path + '/deletion-plan')
    response = a.request('DELETE', path, json={'plan_id': preview.json()['plan_id'], 'confirm': work})
    assert response.status_code == 200, response.text
    assert not folder.exists()
    assert a.get('/api/projects').json()['projects'] == []
    assert a.get('/api/projects/' + item['id']).status_code == 404


def test_delete_segment_updates_plan_without_erasing_shared_assets(private_workbench):
    a, _, ownership, _ = private_workbench
    item = create(a)
    base = '/api/projects/' + item['id']
    assert a.post(base + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'first'}).status_code == 200
    assert a.post(base + '/plan/segments', json={'segment_id': 'S02', 'duration_seconds': 5, 'prompt': 'second'}).status_code == 200
    plan = a.get(base + '/plan').json()
    revision = plan['revision']
    path = base + '/plan/segments/S01'
    assert a.request('DELETE', path, json={'expected_revision': revision, 'confirm': 'wrong'}).status_code == 422
    response = a.request('DELETE', path, json={'expected_revision': revision, 'confirm': 'S01'})
    assert response.status_code == 200, response.text
    after = a.get(base + '/plan').json()
    assert [segment['id'] for segment in after['segments']] == ['S02']
    assert after['segments'][0]['start_seconds'] == 0


def test_series_delete_rejects_another_target_plan(private_workbench):
    a, _, ownership, _ = private_workbench
    assert a.post('/api/private/series', json={'name': '甲系列'}).status_code == 201
    assert a.post('/api/private/series', json={'name': '乙系列'}).status_code == 201
    preview = a.post('/api/private/series/甲系列/deletion-plan').json()
    wrong = a.request('DELETE', '/api/private/series/乙系列', json={'plan_id': preview['plan_id'], 'confirm': '乙系列'})
    assert wrong.status_code == 409
    assert UserContext('user001', ownership.layout).path('series/乙系列').is_dir()
    preview = a.post('/api/private/series/甲系列/deletion-plan').json()
    response = a.request('DELETE', '/api/private/series/甲系列', json={'plan_id': preview['plan_id'], 'confirm': '甲系列'})
    assert response.status_code == 200, response.text
    assert not UserContext('user001', ownership.layout).path('series/甲系列').exists()


def test_external_directory_move_disappears_from_project_list(private_workbench):
    a, _, ownership, _ = private_workbench
    item = create(a)
    context = UserContext('user001', ownership.layout)
    folder = context.project(item['id'])
    holding = context.path('moved-outside-series')
    folder.rename(holding)
    listing = a.get('/api/projects')
    assert listing.status_code == 200
    assert listing.json()['projects'] == []
    assert a.get('/api/projects/' + item['id']).status_code == 404


def test_legacy_registration_can_be_hidden_without_deleting_d_drive_files(private_workbench):
    _, _, ownership, project = private_workbench
    workspace = project / 'workspaces' / 'legacy-work'
    workspace.mkdir(parents=True)
    source = workspace / 'original.txt'
    source.write_text('keep', encoding='utf-8')
    manifest = backend.blank_project_manifest('legacy-work', '旧系列', '旧作品', workspace)
    backend.persist_project_manifest(manifest, make_default=False)

    ordinary = current_principal.set(Principal(SessionIdentity('user001', time.time() + 60), 'test', False))
    try:
        with pytest.raises(HTTPException) as denied:
            backend.retire_legacy_project('legacy-work')
        assert denied.value.status_code == 404
    finally:
        current_principal.reset(ordinary)

    admin = current_principal.set(Principal(SessionIdentity('user000', time.time() + 60), 'test', True))
    try:
        assert any(item['id'] == 'legacy-work' for item in backend.projects()['projects'])
        result = backend.retire_legacy_project('legacy-work')
        assert result == {'retired_project_id': 'legacy-work', 'source_files_preserved': True}
        assert not any(item['id'] == 'legacy-work' for item in backend.projects()['projects'])
        with pytest.raises(HTTPException) as missing:
            ownership.require('legacy-work')
        assert missing.value.status_code == 404
    finally:
        current_principal.reset(admin)
    assert source.read_text(encoding='utf-8') == 'keep'


def test_upload_media_cross_account_and_legacy_denial(private_workbench):
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    response = a.post('/api/projects/' + first['id'] + '/upload', files=[('files', ('sample.wav', b'private-audio', 'audio/wav'))])
    assert response.status_code == 200, response.text
    manifest = a.get('/api/projects/' + first['id']).json()
    path = next(iter(manifest['assets'][0]['sources'].values()))
    assert a.get('/media-file', params={'path': path}).content == b'private-audio'
    assert b.get('/media-file', params={'path': path}, headers={'Range': 'bytes=0-2'}).status_code == 403
    assert b.post('/api/projects/' + first['id'] + '/upload', files=[('files', ('bad.wav', b'bad'))]).status_code == 404
    for path in ('/api/tasks', '/api/events', '/api/plan', '/media/sample.wav'):
        assert b.get(path).status_code == 403
    assert b.post('/api/resources/release').status_code == 403
    assert a.get('/api/material-library', params={'project_id': second['id']}).status_code == 404


def test_agent_http_series_upload_project_and_shot_without_browser_headers(private_workbench):
    owner, other, _, _ = private_workbench
    # The fixture authenticates with Bearer only: no Origin or X-Workspace-User.
    series = owner.post('/api/private/series', json={'name': 'Agent流程'})
    assert series.status_code == 201, series.text
    assert owner.post('/api/private/series', json={'name': 'Agent流程'}).status_code == 409
    assert other.get('/api/private/series').json()['series'] == []

    created = owner.post('/api/projects/create', json={'series': 'Agent流程', 'title': '样例', 'select': False})
    assert created.status_code == 200, created.text
    project_id = created.json()['project']['id']
    duplicate = owner.post('/api/projects/create', json={'series': 'Agent流程', 'title': '样例', 'select': False})
    assert duplicate.status_code == 200
    assert duplicate.json()['project']['id'] != project_id

    base = '/api/projects/' + project_id
    upload = owner.post(base + '/upload', files=[('files', ('mouse.png', b'picture', 'image/png'))])
    assert upload.status_code == 200, upload.text
    assert len(upload.json()['project']['assets']) == 1
    repeated = owner.post(base + '/upload', files=[('files', ('mouse.png', b'picture', 'image/png'))])
    assert repeated.status_code == 200
    assert repeated.json()['uploaded'] != upload.json()['uploaded']
    assert len(repeated.json()['project']['assets']) == 2
    assert owner.post(base + '/upload', files=[('files', ('bad.exe', b'bad', 'application/octet-stream'))]).status_code == 422

    saved = owner.post(base + '/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'prompt': '产品特写', 'expected_revision': 0,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()['segment']['id'] == 'S01'
    assert owner.post(base + '/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'expected_revision': 0,
    }).status_code == 409
    assert [item['id'] for item in owner.get(base + '/plan').json()['segments']] == ['S01']
    assert other.get(base).status_code == 404
    assert other.post(base + '/upload', files=[('files', ('mouse.png', b'bad', 'image/png'))]).status_code == 404


def test_uploaded_reference_keeps_filename_and_never_becomes_generated_shot(private_workbench):
    owner, other, _, _ = private_workbench
    project_id = create(owner)['id']
    base = '/api/projects/' + project_id
    upload = owner.post(base + '/upload', files=[
        ('files', ('examples/transparent_rgb_gaming_mouse.png', b'first-frame', 'image/png')),
        ('files', ('video_minimax_h3_i2v.mp4', b'reference-video', 'video/mp4')),
    ])
    assert upload.status_code == 200, upload.text
    assets = upload.json()['project']['assets']
    assert [asset['original_filename'] for asset in assets] == [
        'transparent_rgb_gaming_mouse.png', 'video_minimax_h3_i2v.mp4',
    ]
    assert [asset['title'] for asset in assets] == [asset['original_filename'] for asset in assets]
    assert all(name not in path for name, path in zip((asset['title'] for asset in assets), upload.json()['uploaded']))
    assert owner.get('/media-file', params={'path': upload.json()['uploaded'][1]}).content == b'reference-video'

    shot = owner.post(base + '/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'expected_revision': 0,
        'first_frame': upload.json()['uploaded'][0],
    })
    assert shot.status_code == 200, shot.text
    assert shot.json()['segment']['status'] == 'planned'
    assert shot.json()['segment'].get('video') is None
    assert owner.get(base + '/segments/S01/versions').json()['items'] == []
    assert owner.get(base + '/plan').json()['assembly'] is None

    old_title = assets[1]['title']
    renamed = owner.patch(base + '/assets/' + assets[1]['id'] + '/label', json={
        'title': '官方参考成片', 'expected_title': old_title,
    })
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()['asset']['title'] == '官方参考成片'
    assert renamed.json()['asset']['original_filename'] == old_title
    assert owner.get(base).json()['assets'][1]['title'] == '官方参考成片'
    assert owner.patch(base + '/assets/' + assets[1]['id'] + '/label', json={
        'title': '过期覆盖', 'expected_title': old_title,
    }).status_code == 409
    assert other.patch(base + '/assets/' + assets[1]['id'] + '/label', json={
        'title': '越权修改', 'expected_title': '官方参考成片',
    }).status_code == 404
    assert owner.get('/media-file', params={'path': upload.json()['uploaded'][1]}).content == b'reference-video'


def test_unknown_legacy_projects_not_auto_assigned(private_workbench):
    a, b, _, project = private_workbench
    legacy = backend.blank_project_manifest('legacy', 'old', 'old', project / 'old')
    backend.persist_project_manifest(legacy, make_default=False)
    for client in (a, b):
        assert client.get('/api/projects/legacy').status_code == 404
        assert client.get('/api/projects').json()['projects'] == []
    assert json.loads(backend.PROJECT_CATALOG_PATH.read_text())['projects'][0]['id'] == 'legacy'


def test_real_mcp_protocol_keeps_user_identity(private_workbench):
    pytest.importorskip('mcp')
    import httpx
    from mcp import Client
    from mcp_server.server import create_server
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)

    async def verify():
        transport = httpx.ASGITransport(app=backend.app)
        for browser, own, other in ((a, first, second), (b, second, first)):
            server = create_server('http://localhost', transport, session_token=browser.headers['Authorization'][7:])
            async with Client(server) as client:
                result = (await client.call_tool('list_projects', {})).structured_content
                assert [p['id'] for p in result['data']['projects']] == [own['id']]
                result = (await client.call_tool('read_project', {'project_id': other['id']})).structured_content
                assert result['error']['http_status'] == 404
        async with Client(create_server('http://localhost', transport)) as client:
            result = (await client.call_tool('list_projects', {})).structured_content
            assert result['error']['http_status'] == 401
    asyncio.run(verify())


def test_cpu_task_receipt_owner_and_cross_user_controls(private_workbench, monkeypatch):
    a, b, _, _ = private_workbench
    from backend.task_scheduler import TaskScheduler
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('CPU queue contacted ComfyUI'))
    scheduler = TaskScheduler(lambda: backend.db(), backend.DB_LOCK, backend.GPU_ADMISSION_LOCK,
                              lambda: pytest.fail('CPU queue consulted GPU resources'),
                              lambda task: backend.revalidate_queued_task(task),
                              lambda task: backend.execute_scheduled_task(task))
    monkeypatch.setattr(backend, 'TASK_SCHEDULER', scheduler)
    first, second = create(a), create(b)
    content = io.BytesIO()
    with wave.open(content, 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x10\x00' * 8000)
    tasks = []
    for client, project in ((a, first), (b, second)):
        base = '/api/projects/' + project['id']
        uploaded = client.post(base + '/upload', files=[('files', ('voice.wav', content.getvalue(), 'audio/wav'))])
        assert uploaded.status_code == 200, uploaded.text
        asset = client.get(base).json()['assets'][0]['id']
        response = client.post(base + '/audio-edit-tasks', json={'source_asset_id': asset, 'idempotency_key': 'same-key', 'gain': .5})
        assert response.status_code == 202, response.text
        task = response.json()
        assert task['status'] == 'scheduler_waiting'
        assert scheduler.tick(threaded=False) == [task['id']]
        task = client.get(base + '/tasks/' + task['id']).json()
        assert task['status'] == 'succeeded', task
        tasks.append(task)
        assert client.get(base + '/submission-receipt', params={'key': 'same-key'}).json()['id'] == task['id']
        other = b if client is a else a
        for suffix in ('', '/stop', '/reconcile', '/resume'):
            response = other.get(base + '/tasks/' + task['id']) if not suffix else other.post(base + '/tasks/' + task['id'] + suffix)
            assert response.status_code == 404
    assert tasks[0]['id'] != tasks[1]['id']
    assert [task['payload']['owner_user'] for task in tasks] == ['user001', 'user002']
    assert tasks[0]['result']['output'] != tasks[1]['result']['output']


def test_generated_result_publication_uses_frozen_owner_without_login(private_workbench):
    a, b, ownership, _ = private_workbench
    first, second = create(a), create(b)
    task = {'id': str(uuid.uuid4()), 'project_id': first['id'],
            'payload': {'owner_user': 'user001', 'project_id': first['id']}}
    engine_file = backend.OUTPUT_ROOT / '导演工作台工作目录' / first['id'] / 'video' / 'result.mp4'
    engine_file.parent.mkdir(parents=True)
    engine_file.write_bytes(b'publication-fixture-not-real-video')
    receipt = {'outputs': [engine_file.relative_to(backend.OUTPUT_ROOT).as_posix()]}
    # No request principal here: same condition as a worker after user logout.
    result = backend.publish_task_outputs(task, receipt)
    published = backend.ROOT / result['published_outputs'][0]
    assert published.read_bytes() == engine_file.read_bytes()
    assert 'user001' in published.parts
    assert backend.publish_task_outputs(task, receipt) == result
    assert b.get('/media-file', params={'path': str(published)}).status_code == 403
    assert a.get('/media-file', params={'path': str(published)}).content == engine_file.read_bytes()
    wrong = {**task, 'payload': {**task['payload'], 'owner_user': 'user002'}}
    with pytest.raises(HTTPException) as error:
        backend.publish_task_outputs(wrong, receipt)
    assert error.value.status_code == 409
    foreign = backend.OUTPUT_ROOT / '导演工作台工作目录' / second['id'] / 'result.mp4'
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b'other')
    with pytest.raises(HTTPException):
        ownership.publish(task, foreign, backend.OUTPUT_ROOT)
    assert engine_file.is_file()


def test_task_owner_mismatch_blocks_recovery_before_engine(private_workbench, monkeypatch):
    a, _, _, _ = private_workbench
    first = create(a)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('Contacted engine for wrong owner'))
    task = {'id': str(uuid.uuid4()), 'project_id': first['id'], 'status': 'needs_reconcile',
            'payload': {'kind': 'keyframe', 'owner_user': 'user002', 'project_id': first['id']}}
    with pytest.raises(HTTPException) as error:
        backend.reconcile_task(task)
    assert error.value.status_code == 409


def test_admin_legacy_registration_does_not_grant_other_private_files(private_workbench):
    a, b, _, project = private_workbench
    private = create(b)
    legacy_root = project / 'legacy'
    legacy_root.mkdir()
    media = legacy_root / 'image.png'
    media.write_bytes(b'legacy-media')
    legacy = backend.blank_project_manifest('old', 'old', 'old', legacy_root)
    legacy['media'] = {'image': str(media)}
    backend.persist_project_manifest(legacy, make_default=False)
    admin = TestClient(backend.app)
    assert admin.post('/api/auth/login', json={'username': 'user000', 'password': 'admin'}).status_code == 200
    assert admin.get('/api/projects/old').status_code == 200
    assert admin.get('/media-file', params={'path': str(media)}).content == b'legacy-media'
    assert a.get('/api/projects/old').status_code == 404
    assert a.get('/media-file', params={'path': str(media)}).status_code == 403
    assert admin.get('/api/projects/' + private['id']).status_code == 404
    private_manifest = backend.ROOT / private['workspace_root'] / 'director.project.json'
    assert admin.get('/media-file', params={'path': str(private_manifest)}).status_code == 403
    unrelated = project / 'unregistered.json'
    unrelated.write_text('{"secret": true}')
    assert admin.get('/media-file', params={'path': str(unrelated)}).status_code == 403
    output = backend.ROOT / 'ComfyUI-Shared/output/legacy-series/old.mp4'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'legacy-result')
    snapshot = project / 'workflows/snapshots/old.json'
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('{}')
    plan = backend.ROOT / legacy['plan_path']
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'segments': [{'id': 'S01', 'video': {'path': 'output/legacy-series/old.mp4'}, 'workflow': str(snapshot)}]}))
    assert admin.get('/media-file', params={'path': str(output)}).content == b'legacy-result'
    assert admin.get('/workflow-file', params={'path': str(snapshot)}).status_code == 200
    unrelated_graph = snapshot.with_name('unregistered.json')
    unrelated_graph.write_text('{}')
    assert admin.get('/workflow-file', params={'path': str(unrelated_graph)}).status_code == 403


def test_upload_cannot_replace_manifest_or_plan_and_workflow_override_denied(private_workbench):
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    base = '/api/projects/' + first['id']
    before = a.get(base).json()
    hostile = json.dumps({**before, 'workspace_root': second['workspace_root'], 'plan_path': second['plan_path']}).encode()
    response = a.post(base + '/upload', files=[('files', ('director.project.json', hostile, 'application/json'))])
    assert response.status_code == 200, response.text
    after = a.get(base).json()
    assert after['workspace_root'] == before['workspace_root']
    assert after['plan_path'] == before['plan_path']
    assert '/assets/uploads/' in response.json()['uploaded'][0]
    response = a.post(base + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'test'})
    assert response.status_code == 200, response.text
    path = response.json()['segment'].get('workflow', '')
    response = a.patch(base + '/plan/segments/S01', json={'workflow': 'ComfyUI-Workspace/evil.json'})
    assert response.status_code == 403
    assert a.get(base + '/plan').json()['segments'][0].get('workflow', '') == path
    response = a.post(base + '/plan/segments', json={'segment_id': 'S02', 'duration_seconds': 5, 'prompt': 'test', 'workflow': 'evil.json'})
    assert response.status_code == 403


def test_background_rejects_corrupt_manifest_root_and_relative_comfy_input(private_workbench):
    from backend.private_auth import Principal, current_principal
    from backend.user_context import SessionIdentity
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    foreign_input = backend.INPUT_ROOT / 'foreign.wav'
    foreign_output = backend.OUTPUT_ROOT / 'foreign.wav'
    for path in (foreign_input, foreign_output):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'other-user')
    context = current_principal.set(Principal(SessionIdentity('user001', time.time() + 60), 'fixture', False))
    try:
        for reference in ('foreign.wav', 'foreign.wav [output]'):
            with pytest.raises(HTTPException) as error:
                backend.stage_comfy_input_files({'1': {'class_type': 'LoadAudio', 'inputs': {'audio': reference}}}, first['id'])
            assert error.value.status_code == 403
    finally:
        current_principal.reset(context)
    manifest_path = backend.PROJECTS_ROOT / (first['id'] + '.json')
    document = json.loads(manifest_path.read_text())
    document['workspace_root'] = second['workspace_root']
    manifest_path.write_text(json.dumps(document))
    with pytest.raises(HTTPException) as error:
        backend.load_project_manifest(first['id'])
    assert error.value.status_code == 409


def test_real_junction_range_head_and_long_path_boundary(private_workbench):
    from backend.user_context import UserContext
    a, b, ownership, _ = private_workbench
    first, second = create(a), create(b)
    own_root = backend.ROOT / first['workspace_root']
    other_root = backend.ROOT / second['workspace_root']
    source = own_root / 'sample.wav'
    source.write_bytes(b'0123456789')
    foreign = other_root / 'secret.wav'
    foreign.write_bytes(b'foreign')
    link = own_root / 'linked'
    command = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Target '" + str(other_root).replace("'", "''") + "' | Out-Null"
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], check=True, capture_output=True)
    assert link.resolve() == other_root.resolve()
    assert a.get('/media-file', params={'path': str(source)}, headers={'Range': 'bytes=1-3'}).content == b'123'
    assert b.get('/media-file', params={'path': str(source)}, headers={'Range': 'bytes=1-3'}).status_code == 403
    assert a.get('/media-file', params={'path': str(link / 'secret.wav')}).status_code == 403
    # HEAD is unsupported; a built UI's catch-all mount returns 404 rather
    # than the API-only router's 405. Neither may expose the private file.
    head = b.head('/media-file', params={'path': str(source)})
    assert head.status_code in {403, 404, 405}
    assert head.content == b''
    assert 'content-range' not in head.headers
    for path in (own_root / '..' / second['id'] / 'secret.wav', own_root / 'sample.wav:stream', own_root / 'CON'):
        assert a.get('/media-file', params={'path': str(path)}).status_code == 403
    context = UserContext('user001', ownership.layout)
    with pytest.raises(ValueError, match='too long'):
        context.path(own_root / ('x' * 180) / 'file.wav')
    assert not (own_root / ('x' * 180)).exists()
