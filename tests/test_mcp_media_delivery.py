"""Issue #120: remote reference import, controlled observation refs and media delivery.

These tests use the real private HTTP app, the real Streamable HTTP MCP mount and
the real signed-media routes. They do not start a GPU, ComfyUI or a second server:
media probing and extraction are replaced by project-owned fixture files.
"""
import asyncio
import base64
import json
import re
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend import app as backend
from backend import media_refs, remote_mcp
from mcp_server.server import create_server
from support import create, seed_test_models
from test_private_workbench import private_workbench

SERVER_PATH = re.compile(r'(?i)(?:[a-z]:[\\/]|ComfyUI-Workspace|ComfyUI-Shared|private-workspaces|tmp[\\/])')
FACTS = {'duration_seconds': 2.0, 'width': 640, 'height': 360, 'fps': 24.0,
         'has_video': True, 'has_audio': True, 'audio_channels': 2, 'sample_rate': 48000}


def assert_no_server_path(value) -> None:
    text = json.dumps(value, ensure_ascii=False)
    assert not SERVER_PATH.search(text), text


def body(result):
    return result.structured_content


@pytest.fixture
def remote_access(tmp_path_factory, monkeypatch):
    tmp = tmp_path_factory.mktemp('issue120')
    a, b, _ownership, _root = private_workbench.__wrapped__(tmp, monkeypatch)
    seed_test_models(backend.SHARED_ROOT)
    monkeypatch.setattr(backend, 'queue_task', lambda *args: None)
    monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: {
        'queue_running': [], 'queue_pending': []})
    backend.app.router.routes = [route for route in backend.app.router.routes
                                 if getattr(route, 'path', None) != '/mcp']
    server = remote_mcp.install(backend.app, http_app_provider=lambda: backend.app,
                                token_provider=lambda: (backend.current_principal.get().token
                                                        if backend.current_principal.get() else None))
    mount = next(route for route in backend.app.router.routes if getattr(route, 'path', None) == '/mcp')
    backend.app.router.routes.remove(mount)
    backend.app.router.routes.insert(0, mount)
    monkeypatch.setattr(backend, 'REMOTE_MCP', server)
    return a, b


@asynccontextmanager
async def remote(token):
    """Open a real Streamable HTTP MCP session with the caller's bearer."""
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=backend.app),
                                  headers={'Authorization': token}, timeout=10) as http:
        async with streamable_http_client('http://localhost:4100/mcp/', http_client=http) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as client:
                await client.initialize()
                yield client


def upload_audio(client, project_id: str, content: bytes, name: str = 'clip.wav'):
    response = client.post(f'/api/projects/{project_id}/upload',
                           files=[('files', (name, content, 'audio/wav'))])
    assert response.status_code == 200, response.text
    return response.json()['project']['assets'][0]


def test_media_refs_are_signed_project_scoped_and_path_free(remote_access, monkeypatch):
    a, b = remote_access
    project = create(a)
    project_id = project['id']
    asset = upload_audio(a, project_id, b'audio-fixture-bytes')

    issued = a.post(f'/api/projects/{project_id}/media-refs',
                    json={'media_refs': [f"asset:{asset['id']}:A"]})
    assert issued.status_code == 200, issued.text
    entry = issued.json()['refs'][0]
    assert entry['ok'] and entry['media_ref'] == f"asset:{asset['id']}:A"
    assert entry['url'].startswith(f'/api/projects/{project_id}/media/')
    assert entry['kind'] == 'audio' and entry['size_bytes'] == len(b'audio-fixture-bytes')
    assert_no_server_path(issued.json())

    assert a.get(entry['url']).content == b'audio-fixture-bytes'
    assert b.get(entry['url']).status_code == 404
    assert TestClient(backend.app).get(entry['url']).status_code == 401

    token = entry['url'].rsplit('/', 1)[-1]
    tampered = token[:-1] + ('A' if token[-1] != 'A' else 'B')
    assert a.get(f'/api/projects/{project_id}/media/{tampered}').status_code == 404
    assert a.get(f'/api/projects/{project_id}/media/not-a-token').status_code == 404

    now = time.time()
    short = a.post(f'/api/projects/{project_id}/media-refs',
                   json={'media_refs': [f"asset:{asset['id']}:A"], 'ttl_seconds': 60})
    assert short.status_code == 200, short.text
    monkeypatch.setattr(media_refs, 'time', types.SimpleNamespace(time=lambda: now + 120))
    assert a.get(short.json()['refs'][0]['url']).status_code == 410
    monkeypatch.setattr(media_refs, 'time', time)

    listing = a.get(f'/api/projects/{project_id}/media', params={'category': 'assets'})
    assert listing.status_code == 200, listing.text
    rows = listing.json()['media']
    assert any(row['media_ref'] == f"asset:{asset['id']}:A" for row in rows)
    assert all('path' not in row and row['url'].startswith('/api/') for row in rows)
    assert_no_server_path(listing.json())

    other = create(b)
    assert b.get(f'/api/projects/{project_id}/media').status_code == 404
    assert b.post(f'/api/projects/{project_id}/media-refs',
                  json={'media_refs': [f"asset:{asset['id']}:A"]}).status_code == 404
    assert other['id'] != project_id


def test_reference_from_material_registers_facts_without_analysis(remote_access, monkeypatch):
    a, b = remote_access
    monkeypatch.setattr('tools.reference_decompose.media_facts', lambda path: dict(FACTS))
    project = create(a)
    project_id = project['id']
    asset = upload_audio(a, project_id, b'reference-audio')

    first = a.post(f'/api/projects/{project_id}/reverse-analyses/from-material',
                   json={'asset_id': asset['id']})
    assert first.status_code == 201, first.text
    registered = first.json()
    assert registered['created'] is True
    analysis = registered['analysis']
    assert analysis['status'] == 'registered'
    assert analysis['observations'] == [] and analysis['timeline'] == []
    assert analysis['source']['material_asset_id'] == asset['id']
    assert analysis['stages'][-1]['id'] == 'production' and analysis['stages'][-1]['status'] == 'waiting'

    repeated = a.post(f'/api/projects/{project_id}/reverse-analyses/from-material',
                      json={'asset_id': asset['id']})
    assert repeated.status_code == 201
    assert repeated.json()['created'] is False
    assert repeated.json()['analysis']['id'] == analysis['id']

    assert b.post(f'/api/projects/{project_id}/reverse-analyses/from-material',
                  json={'asset_id': asset['id']}).status_code == 404
    assert a.post(f'/api/projects/{project_id}/reverse-analyses/from-material',
                  json={'asset_id': 'missing-asset'}).status_code == 404
    image = a.post(f'/api/projects/{project_id}/upload',
                   files=[('files', ('frame.png', b'png', 'image/png'))]).json()['project']['assets'][-1]
    assert a.post(f'/api/projects/{project_id}/reverse-analyses/from-material',
                  json={'asset_id': image['id']}).status_code == 422


def _fake_extract_range(source, directory, facts, start, end, frame_seconds=None, artifact_id=None):
    directory.mkdir(parents=True, exist_ok=True)
    segment = directory / f'{artifact_id}-segment.mp4'
    segment.write_bytes(b'segment-fixture')
    frame = None
    if frame_seconds is not None:
        frame = directory / f'{artifact_id}-frame.png'
        frame.write_bytes(b'frame-fixture')
    return {'segment_path': segment, 'frame_path': frame,
            'start_seconds': start, 'end_seconds': end}


def _fake_extract_frames(source, directory, facts, times, sample_id=None):
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, value in enumerate(times):
        path = directory / f'{sample_id}-{index}.png'
        path.write_bytes(b'frame-fixture')
        frames.append({'time_seconds': value, 'frame_path': path})
    return frames


def _fake_extract_audio(source, directory, facts, start, end, artifact_id=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{artifact_id}-audio.wav'
    path.write_bytes(b'audio-fixture')
    return path


def test_remote_mcp_reference_flow_and_media_delivery(remote_access, monkeypatch, tmp_path):
    a, _b = remote_access
    monkeypatch.setattr('tools.reference_decompose.media_facts', lambda path: dict(FACTS))
    monkeypatch.setattr('tools.reference_decompose.extract_explicit_range', _fake_extract_range)
    monkeypatch.setattr('tools.reference_decompose.extract_explicit_frames', _fake_extract_frames)
    monkeypatch.setattr('tools.reference_decompose.extract_explicit_audio', _fake_extract_audio)
    project = create(a)
    project_id = project['id']

    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                uploaded = body(await client.call_tool('upload_material', {
                    'project_id': project_id, 'filename': 'reference.wav',
                    'content_base64': base64.b64encode(b'reference-bytes').decode()}))
                assert uploaded['ok'], uploaded
                asset_id = uploaded['data']['project']['assets'][0]['id']

                imported = body(await client.call_tool('import_reference_from_material', {
                    'project_id': project_id, 'asset_id': asset_id}))
                assert imported['ok'], imported
                analysis_id = imported['data']['analysis']['id']
                assert imported['data']['created'] is True
                assert imported['data']['analysis']['observations'] == []
                assert_no_server_path(imported)

                ranged = body(await client.call_tool('extract_reference_range', {
                    'project_id': project_id, 'analysis_id': analysis_id,
                    'start_seconds': 0.2, 'end_seconds': 1.4, 'frame_seconds': 0.8}))
                assert ranged['ok'], ranged
                observation = ranged['data']['observation']
                assert observation['id'].startswith('range-')
                assert observation['start_seconds'] == 0.2 and observation['end_seconds'] == 1.4
                assert observation['provenance'] == 'observable_fact' and observation['is_shot_cut'] is False
                assert {item['kind'] for item in observation['media']} == {'video', 'image'}
                assert all(item['url'].startswith('/api/') for item in observation['media'])
                assert ranged['data']['analysis']['revision'] >= 1
                assert_no_server_path(ranged)

                sampled = body(await client.call_tool('sample_reference_frames', {
                    'project_id': project_id, 'analysis_id': analysis_id,
                    'times_seconds': [0.1, 0.5]}))
                assert sampled['ok'] and sampled['data']['observation']['sampling_mode'] == 'caller_selected'
                assert all('path' not in frame for frame in sampled['data']['observation']['frames'])
                assert all('media' in frame for frame in sampled['data']['observation']['frames'])
                assert sampled['data']['observation']['frames'][0]['media']['media_ref'].endswith(':frame_0')
                assert_no_server_path(sampled)

                audio = body(await client.call_tool('extract_reference_audio', {
                    'project_id': project_id, 'analysis_id': analysis_id,
                    'start_seconds': 0.0, 'end_seconds': 1.0}))
                assert audio['ok'] and audio['data']['observation']['media'][0]['kind'] == 'audio'
                assert_no_server_path(audio)

                reference = body(await client.call_tool('read_reference', {
                    'project_id': project_id, 'analysis_id': analysis_id}))
                assert reference['ok'], reference
                assert len(reference['data']['observations']) == 3
                assert reference['data']['source']['material_asset_id'] == asset_id
                assert all('path' not in observation for observation in reference['data']['observations'])
                assert reference['data']['source']['media']['media_ref'] == f'reference-source:{analysis_id}'
                assert_no_server_path(reference)

                media = body(await client.call_tool('list_media', {
                    'project_id': project_id, 'category': 'references', 'limit': 100}))
                assert media['ok'], media
                refs = [row['media_ref'] for row in media['data']['media']]
                assert f'reference-source:{analysis_id}' in refs
                assert any(ref.startswith(f'reference:{analysis_id}:') for ref in refs)
                assert_no_server_path(media)

                ref = f'asset:{asset_id}:A'
                url = body(await client.call_tool('get_media_url', {
                    'project_id': project_id, 'media_ref': ref}))
                assert url['ok'], url
                assert url['data']['url'].startswith(f'/api/projects/{project_id}/media/')
                delivered = body(await client.call_tool('download_media', {
                    'project_id': project_id, 'media_ref': ref}))
                assert delivered['ok'], delivered
                assert delivered['data']['inline'] is True
                assert base64.b64decode(delivered['data']['content_base64']) == b'reference-bytes'
                assert_no_server_path(delivered)

                over = body(await client.call_tool('import_reference_content', {
                    'project_id': project_id, 'filename': 'huge.mp4',
                    'content_base64': 'A' * (22_369_625)}))
                assert over['ok'] is False
                assert over['error']['code'] == 'reference_too_large'
                assert 'HTTP multipart' in over['error']['message']
                assert 'http_fallback' in over['error']

    asyncio.run(verify())


def test_remote_mcp_large_media_returns_url_instead_of_base64(remote_access):
    a, _b = remote_access
    project = create(a)
    project_id = project['id']
    big = b'x' * 1_200_000
    upload = a.post(f'/api/projects/{project_id}/upload',
                    files=[('files', ('large.wav', big, 'audio/wav'))])
    assert upload.status_code == 200, upload.text
    asset_id = upload.json()['project']['assets'][0]['id']

    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                delivered = body(await client.call_tool('download_media', {
                    'project_id': project_id, 'media_ref': f'asset:{asset_id}:A'}))
                assert delivered['ok'], delivered
                assert delivered['data']['inline'] is False
                assert 'content_base64' not in delivered['data']
                assert delivered['data']['url'].startswith('/api/')
                assert delivered['data']['size_bytes'] == len(big)
    asyncio.run(verify())


def test_mcp_reference_content_import_uses_content_not_server_path(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(422, json={'detail': 'probe failed'})

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond),
                                        remote=True)) as client:
            result = body(await client.call_tool('import_reference_content', {
                'project_id': 'work-a', 'filename': 'clip.mp4',
                'content_base64': base64.b64encode(b'video-bytes').decode()}))
            assert result['error']['http_status'] == 422
            assert requests[0].url.path == '/api/projects/work-a/reverse-analyses'
            assert b'video-bytes' in requests[0].content
            over = body(await client.call_tool('import_reference_content', {
                'project_id': 'work-a', 'filename': 'clip.mp4',
                'content_base64': 'A' * 22_369_625}))
            assert over['error']['code'] == 'reference_too_large'
            assert len(requests) == 1

    asyncio.run(verify())


def test_remote_mcp_task_receipts_are_path_free_and_keep_ids(remote_access, monkeypatch):
    a, _b = remote_access
    project = create(a)
    project_id = project['id']
    task_id = 'task-issue120'
    output = Path(project['workspace_root']) / 'outputs' / 'result.mp4'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'result-video')
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, 'S01', 'succeeded', time.time(), time.time(),
             json.dumps({'kind': 'workflow', 'project_id': project_id, 'owner_user': 'user001',
                         'workflow': str(backend.ROOT / 'workflows' / 'graph.json'),
                         'submission_receipt': {'key': 'agent-key', 'request': {}}}),
             json.dumps({'status': 'success', 'outputs': ['0.mp4'],
                         'published_outputs': [str(output)],
                         'width': 768, 'height': 1344}), project_id))
        connection.commit()

    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                receipt = body(await client.call_tool('get_task', {'project_id': project_id,
                                                                   'task_id': task_id}))
                assert receipt['ok'], receipt
                assert receipt['data']['id'] == task_id
                assert receipt['data']['status'] == 'succeeded'
                assert receipt['data']['payload']['submission_receipt']['key'] == 'agent-key'
                assert receipt['data']['media'][0]['media_ref'] == f'task:{task_id}:video:0'
                assert receipt['data']['candidate_ref'] == f'task:{task_id}:video:0'
                assert_no_server_path(receipt)

                listed = body(await client.call_tool('list_tasks', {'project_id': project_id}))
                assert listed['ok'] and listed['data']['tasks'][0]['id'] == task_id
                assert_no_server_path(listed)

                with backend.db() as connection:
                    connection.execute('DELETE FROM tasks WHERE id=?', (task_id,))
                    connection.commit()

    asyncio.run(verify())





