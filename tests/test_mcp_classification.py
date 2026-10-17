import asyncio
import json

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server


@pytest.mark.parametrize('status', [200, 409, None])
def test_classification_schema_forwarding_and_no_retry(status):
    requests = []
    args = dict(project_id='work', analysis_id='ref-1', content_mode='showcase', reason='单角色展示而非故事推进', expected_revision=4)
    def respond(request):
        requests.append(request)
        if status is None:
            raise httpx.ReadTimeout('unknown', request=request)
        return httpx.Response(status, json={'analysis': {'revision': 5}} if status == 200 else {'detail': 'revision conflict'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == 'correct_reference_classification')
            assert {'reason', 'expected_revision', 'content_mode'} <= set(tool.input_schema['required'])
            assert set(tool.input_schema['properties']['content_mode']['enum']) == {'narrative', 'music_performance', 'dance', 'showcase', 'mood', 'technical', 'other'}
            result = (await client.call_tool(tool.name, args)).structured_content
            assert result['ok'] is (status == 200)
            if status == 409:
                assert result['error']['http_status'] == 409
            elif status is None:
                assert result['error']['outcome_unknown'] and not result['error']['retryable']
    asyncio.run(verify())
    assert len(requests) == 1
    assert requests[0].method == 'PATCH'
    assert requests[0].url.path == '/api/projects/work/reverse-analyses/ref-1/classification'
    assert json.loads(requests[0].content) == {k: args[k] for k in ('content_mode', 'reason', 'expected_revision')}


@pytest.mark.parametrize('override', [{'reason': '  '}, {'reason': 'x' * 2001}, {'expected_revision': -1}, {'content_mode': 'unknown'}, {'analysis_id': '../x'}])
def test_classification_invalid_input_does_not_write(override):
    def respond(request):
        pytest.fail('Invalid classification reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = await client.call_tool('correct_reference_classification', dict(
                project_id='work', analysis_id='ref-1', content_mode='showcase', reason='checked', expected_revision=0) | override)
            assert result.is_error
    asyncio.run(verify())
