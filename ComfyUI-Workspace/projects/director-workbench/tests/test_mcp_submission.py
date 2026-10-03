import asyncio
import threading
import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from backend import app as backend
from mcp_server.server import create_server
from test_production_presets import preset_client, install, add_shot

REAL_THREAD = threading.Thread


def test_mcp_generation_retry_creates_one_task(preset_client, monkeypatch):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01')
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'run_workflow_submission', lambda *args: None)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as mcp:
            arguments = {'project_id': 'preset-test', 'asset_id': 'S01', 'idempotency_key': 'mcp-proof'}
            first = await mcp.call_tool('submit_shot', arguments)
            assert first.structured_content['ok'], first
            again = await mcp.call_tool('submit_shot', arguments)
            assert again.structured_content['data']['id'] == first.structured_content['data']['id']
            conflict = await mcp.call_tool('submit_shot', {**arguments, 'prompt': 'different'})
            assert conflict.structured_content['error']['detail']['code'] == 'idempotency_key_conflict'
    asyncio.run(verify())
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


@pytest.mark.parametrize('status', [401, 404, 409, 422])
def test_submission_preserves_backend_failure_without_probe_or_retry(status):
    seen = []
    def respond(request):
        seen.append(request)
        assert request.method == 'POST' and request.url.path == '/api/projects/a/tasks'
        return httpx.Response(status, json={'detail': {'code': 'backend_reason'}})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = (await client.call_tool('submit_shot', {
                'project_id': 'a', 'asset_id': 'S01', 'idempotency_key': 'key', 'expected_revision': 7,
            })).structured_content
            assert result['error']['http_status'] == status
            assert result['error']['detail']['code'] == 'backend_reason'
            assert not result['error']['retryable']
    asyncio.run(verify())
    assert len(seen) == 1
    import json
    assert json.loads(seen[0].content)['expected_revision'] == 7


@pytest.mark.parametrize('tool, arguments, method, suffix, expected_fields', [
    ('update_shot', {'asset_id': 'S01', 'expected_revision': 7,
                     'changes': {'generation': {'aspect_ratio': '1:1', 'seed': 89}}},
     'PATCH', '/plan/segments/S01', {'expected_revision': 7,
                                   'generation': {'aspect_ratio': '1:1', 'seed': 89}}),
    ('arrange_shot', {'asset_id': 'S01', 'expected_revision': 7, 'operation': 'copy'},
     'POST', '/plan/segments/S01/operate', {'operation': 'copy', 'expected_revision': 7}),
    ('convert_reference_to_shots', {'analysis_id': 'ref-1', 'expected_revision': 7},
     'POST', '/reverse-analyses/ref-1/convert', {'expected_revision': 7}),
    ('submit_batch', {'asset_ids': ['S01', 'S02'], 'idempotency_key': 'original-key',
                      'expected_revision': 7},
     'POST', '/batches', {'asset_ids': ['S01', 'S02'], 'idempotency_key': 'original-key',
                        'expected_revision': 7}),
])
@pytest.mark.parametrize('status', [200, 401, 404, 409, 422, None])
def test_current_contract_write_is_one_scoped_request_without_probe(
    tool, arguments, method, suffix, expected_fields, status,
):
    import json
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers['authorization'] == 'Bearer local-session'
        assert request.method == method
        assert request.url.path == '/api/projects/work' + suffix
        body = json.loads(request.content)
        assert all(body[key] == value for key, value in expected_fields.items())
        if status is None:
            raise httpx.ReadTimeout('receipt lost', request=request)
        return httpx.Response(status, json={'revision': 8} if status == 200 else {'detail': 'backend reason'})

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond),
                                        session_token='local-session')) as client:
            result = (await client.call_tool(tool, {'project_id': 'work', **arguments})).structured_content
            assert result['ok'] == (status == 200)
            if status is None:
                assert result['error']['outcome_unknown'] and not result['error']['retryable']
            elif status != 200:
                assert result['error']['http_status'] == status
                assert result['error']['detail'] == 'backend reason'

    asyncio.run(verify())
    assert len(seen) == 1
