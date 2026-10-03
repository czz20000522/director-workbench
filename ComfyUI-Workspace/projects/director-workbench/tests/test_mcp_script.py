import asyncio
import json

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server
from test_production_presets import preset_client
import threading

REAL_THREAD = threading.Thread


SCRIPT = dict(project_id='work', expected_revision=2, title='铜钱', text='剧本正文',
              target_duration_seconds=180, scenes=[{'id': 'SC01', 'title': '卧房', 'duration_seconds': 40}])


@pytest.mark.parametrize('status', [200, 409, None])
def test_script_tools_keep_session_scope_revision_and_unknown_outcome(status):
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers['Authorization'] == 'Bearer test-session'
        assert request.url.path == '/api/projects/work/script'
        if request.method == 'GET':
            return httpx.Response(200, json={'revision': 2, 'text': '原文', 'scenes': []})
        body = json.loads(request.content)
        assert body['expected_revision'] == 2 and body['scenes'][0]['id'] == 'SC01'
        assert 'status' not in body and 'project_id' not in body
        if status is None:
            raise httpx.ReadTimeout('lost', request=request)
        return httpx.Response(status, json={'revision': 3} if status == 200 else {'detail': 'stale'})
    async def verify():
        async with Client(create_server('http://localhost', httpx.MockTransport(respond), session_token='test-session')) as client:
            assert (await client.call_tool('read_script', {'project_id': 'work'})).structured_content['data']['revision'] == 2
            result = (await client.call_tool('save_script', SCRIPT)).structured_content
            assert result['ok'] == (status == 200)
            if status == 409:
                assert result['error']['http_status'] == 409
            if status is None:
                assert result['error']['outcome_unknown'] and not result['error']['retryable']
    asyncio.run(verify())
    assert [request.method for request in seen] == ['GET', 'PUT']


@pytest.mark.parametrize('override', [
    {'expected_revision': True}, {'expected_revision': -1},
    {'scenes': [{'id': 'SC01', 'title': 'a'}, {'id': 'SC01', 'title': 'b'}]},
    {'scenes': [{'id': '', 'title': 'a'}]}, {'target_duration_seconds': 0},
])
def test_invalid_script_does_not_write(override):
    def respond(request):
        pytest.fail('Invalid arguments reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost', httpx.MockTransport(respond))) as client:
            assert (await client.call_tool('save_script', SCRIPT | override)).is_error
    asyncio.run(verify())


@pytest.mark.parametrize('tool,scene_id', [('create_shot', 'SC01'), ('update_shot', 'SC02'), ('update_shot', '')])
def test_shot_scene_binding_forwards_two_revisions_without_contract_probe(tool, scene_id):
    writes = []
    def respond(request):
        assert request.url.path.startswith('/api/projects/work/plan/segments')
        assert request.method == ('POST' if tool == 'create_shot' else 'PATCH')
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={'revision': 8})
    async def verify():
        async with Client(create_server('http://localhost', httpx.MockTransport(respond))) as client:
            association = {'script_scene_id': scene_id, 'expected_script_revision': 3}
            args = {'project_id': 'work', 'expected_revision': 7}
            args.update({'segment_id': 'S01', 'duration_seconds': 5, **association} if tool == 'create_shot'
                        else {'asset_id': 'S01', 'changes': association})
            result = (await client.call_tool(tool, args)).structured_content
            assert result['ok']
    asyncio.run(verify())
    assert len(writes) == 1
    assert writes[0]['script_scene_id'] == scene_id
    assert writes[0]['expected_revision'] == 7 and writes[0]['expected_script_revision'] == 3


@pytest.mark.parametrize('tool', ['create_shot', 'update_shot'])
def test_association_without_script_revision_does_not_reach_http(tool):
    def respond(request):
        pytest.fail('Unversioned association reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost', httpx.MockTransport(respond))) as client:
            args = {'project_id': 'work', 'expected_revision': 1}
            args.update({'segment_id': 'S01', 'duration_seconds': 5, 'script_scene_id': 'SC01'} if tool == 'create_shot'
                        else {'asset_id': 'S01', 'changes': {'script_scene_id': ''}})
            assert (await client.call_tool(tool, args)).is_error
    asyncio.run(verify())


def test_mcp_script_and_linked_shot_round_trip_to_real_backend(preset_client, monkeypatch):
    from backend import app as backend
    http, _ = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    async def verify():
        async with Client(create_server('http://localhost', httpx.ASGITransport(app=backend.app))) as client:
            async def call(tool, **args):
                return (await client.call_tool(tool, {'project_id': 'preset-test', **args})).structured_content
            assert (await call('read_script'))['data']['revision'] == 0
            script_args = {key: value for key, value in SCRIPT.items() if key != 'project_id'}
            script_args['expected_revision'] = 0
            saved = await call('save_script', **script_args)
            assert saved['ok'], saved
            assert saved['data']['status'] == 'draft' and saved['data']['revision'] == 1
            created = await call('create_shot', segment_id='S01', expected_revision=0, duration_seconds=5,
                                 script_scene_id='SC01', expected_script_revision=1)
            assert created['ok'], created
            assert created['data']['segment']['script_scene_id'] == 'SC01'
            assert created['data']['segment']['duration_seconds'] == 5
            conflict = await call('save_script', **(script_args | {'expected_revision': 1, 'scenes': []}))
            assert conflict['error']['http_status'] == 409
            plan = (await call('read_project', section='plan'))['data']
            unlinked = await call('update_shot', asset_id='S01', expected_revision=plan['revision'],
                                  changes={'script_scene_id': '', 'expected_script_revision': 1})
            assert unlinked['ok'], unlinked
            saved = await call('save_script', **(script_args | {'expected_revision': 1, 'scenes': []}))
            assert saved['ok'] and saved['data']['revision'] == 2
    asyncio.run(verify())
    assert not http.get('/api/projects/preset-test/tasks').json()['tasks']
