import asyncio

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server


@pytest.mark.parametrize('operation,result', [
    ('stop', {'id': 'old', 'status': 'stop_requested'}),
    ('resume', {'id': 'new', 'status': 'preparing'}),
    ('resume', {'id': 'old', 'status': 'succeeded'}),
])
def test_controls_preserve_actual_task_and_status(operation, result):
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json=result)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            listing = await client.list_tools()
            tool = next(t for t in listing.tools if t.name == operation + '_generation')
            assert set(tool.input_schema['required']) == {'project_id', 'task_id'}
            assert not tool.annotations.read_only_hint
            assert not tool.annotations.idempotent_hint
            actual = (await client.call_tool(operation + '_generation', {'project_id': 'work', 'task_id': 'old'})).structured_content
            assert actual == {'ok': True, 'data': result}
    asyncio.run(verify())
    assert seen == [('POST', '/api/projects/work/tasks/old/' + operation, b'')]


@pytest.mark.parametrize('operation', ['stop', 'resume'])
@pytest.mark.parametrize('status', [404, 409, 503, None])
def test_control_errors_do_not_retry(operation, status):
    seen = []
    def handler(request):
        seen.append(request)
        if status is None:
            raise httpx.ReadTimeout('unknown result', request=request)
        return httpx.Response(status, json={'detail': {'code': 'cannot_control'}})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = (await client.call_tool(operation + '_generation', {'project_id': 'work', 'task_id': 'old'})).structured_content
            assert not result['ok']
            assert not result['error']['retryable']
            if status is None:
                assert result['error']['outcome_unknown'] is True
            else:
                assert result['error']['http_status'] == status
                assert result['error']['detail']['code'] == 'cannot_control'
    asyncio.run(verify())
    assert len(seen) == 1


@pytest.mark.parametrize('operation', ['stop', 'resume'])
@pytest.mark.parametrize('field,value', [('task_id', ''), ('task_id', '..'), ('task_id', '../x'), ('task_id', 'a\\b'), ('project_id', '../other')])
def test_controls_reject_invalid_paths_without_http(operation, field, value):
    def handler(request):
        pytest.fail('Invalid path reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = await client.call_tool(operation + '_generation', {'project_id': 'work', 'task_id': 'old', field: value})
            assert result.is_error
    asyncio.run(verify())


def test_review_final_stage_does_not_imply_approval():
    async def verify():
        async with Client(create_server('http://localhost:4100')) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == 'record_review')
            assert 'final' in tool.input_schema['properties']['stage']['enum']
            assert set(tool.input_schema['properties']['status']['enum']) == {'pending_review', 'changes_requested'}
    asyncio.run(verify())
