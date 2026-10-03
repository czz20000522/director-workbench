import asyncio
import json

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server


@pytest.mark.parametrize('status', [200, 409, 503])
def test_resolution_forwards_confirmation_and_preserves_backend_decision(status):
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(status, json={'id': 'old-task', 'status': 'failed'} if status == 200 else {'detail': 'not verified'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            listing = await client.list_tools()
            tool = next(tool for tool in listing.tools if tool.name == 'resolve_missing_execution')
            assert {'confirm_execution_ended', 'note'} <= set(tool.input_schema['required'])
            result = (await client.call_tool('resolve_missing_execution', {
                'project_id': 'work', 'task_id': 'old-task',
                'confirm_execution_ended': True, 'note': '旧进程已结束，检查输出没有成片'})).structured_content
            assert result['ok'] is (status == 200)
            if status != 200:
                assert result['error']['http_status'] == status
    asyncio.run(verify())
    assert seen == [('POST', '/api/projects/work/tasks/old-task/resolve-missing', {
        'confirm_execution_ended': True, 'note': '旧进程已结束，检查输出没有成片'})]


@pytest.mark.parametrize('changes', [
    {'confirm_execution_ended': False}, {'confirm_execution_ended': 'true'},
    {'confirm_execution_ended': None}, {'note': '  '}, {'note': 'x' * 2001}, {'task_id': '../other'},
])
def test_resolution_rejects_invalid_confirmation_without_http(changes):
    def handler(request):
        pytest.fail('Invalid confirmation must not reach HTTP')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = await client.call_tool('resolve_missing_execution', {
                'project_id': 'work', 'task_id': 'old-task',
                'confirm_execution_ended': True, 'note': 'verified', **changes})
            assert result.is_error
    asyncio.run(verify())
