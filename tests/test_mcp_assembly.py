import asyncio
import json
import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server


def test_assembly_forwards_revision_and_key_without_contract_probe():
    writes = []
    def handler(request):
        assert request.method == 'POST' and request.url.path == '/api/projects/work/assembly'
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'assembly-task', 'status': 'preparing'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = (await client.call_tool('submit_assembly', {
                'project_id': 'work', 'expected_revision': 7, 'idempotency_key': 'original-key'})).structured_content
            assert result['ok'] and result['data']['id'] == 'assembly-task'
    asyncio.run(verify())
    assert writes == [{'expected_revision': 7, 'idempotency_key': 'original-key'}]
