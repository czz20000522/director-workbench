import asyncio
import json
import httpx
import pytest
from test_reverse_workbench import reverse_client


def test_artifact_history_and_conflict(reverse_client):
    client, _, path = reverse_client
    url = '/api/projects/project-a/reverse-analyses/ref-test/artifacts/story'
    original = json.loads(path.read_text(encoding='utf-8'))['artifacts']['story']
    for revision, (summary, status, confidence) in enumerate([('first', 'reviewed', .8), ('pending', 'unreviewed', .4), ('last', 'reviewed', .9)]):
        response = client.patch(url, json=dict(summary=summary, user_status=status, confidence=confidence, expected_revision=revision))
        assert response.status_code == 200
    artifact = response.json()['artifact']
    assert artifact['original_candidate'] == original
    assert [v['summary'] for v in artifact['review_history']] == [original['summary'], 'first', 'pending']
    assert [v['revision'] for v in artifact['review_history']] == [0, 1, 2]
    assert artifact['review_history'][0]['confidence'] is None
    assert artifact['review_history'][1] | {'recorded_at': 0} == dict(summary='first', user_status='reviewed', status='ready', provenance='user_confirmed', confidence=.8, revision=1, recorded_at=0)
    assert all(isinstance(v['recorded_at'], float) for v in artifact['review_history'])
    before = path.read_bytes()
    assert client.patch(url, json=dict(summary='stale', expected_revision=2)).status_code == 409
    assert path.read_bytes() == before
    assert client.patch(url, json=dict(summary='legacy')).status_code == 200
    assert len(json.loads(path.read_text(encoding='utf-8'))['artifacts']['story']['review_history']) == 4


@pytest.mark.parametrize('status', [200, 409, None])
def test_mcp_schema_forwarding_no_retry(status):
    from mcp import Client
    from mcp_server.server import create_server
    requests = []
    def respond(request):
        requests.append(request)
        if status is None:
            raise httpx.ReadTimeout('unknown', request=request)
        return httpx.Response(status, json={'analysis': {'revision': 5}} if status == 200 else {'detail': 'conflict'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond), session_token='artifact-test-session')) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == 'correct_reference_artifact')
            assert {'project_id', 'analysis_id', 'artifact_id', 'summary', 'expected_revision'} <= set(tool.input_schema['required'])
            result = (await client.call_tool(tool.name, dict(project_id='work', analysis_id='ref-1', artifact_id='actions', summary='correction', expected_revision=4, confidence=.8))).structured_content
            assert result['ok'] is (status == 200)
            if status == 409:
                assert result['error']['http_status'] == 409
            elif status is None:
                assert result['error']['outcome_unknown'] and not result['error']['retryable']
    asyncio.run(verify())
    assert len(requests) == 1
    assert requests[0].headers['Authorization'] == 'Bearer artifact-test-session'
    assert requests[0].method == 'PATCH'
    assert requests[0].url.path == '/api/projects/work/reverse-analyses/ref-1/artifacts/actions'
    assert json.loads(requests[0].content) == dict(summary='correction', user_status='reviewed', expected_revision=4, confidence=.8)


@pytest.mark.parametrize('override', [{'expected_revision': -1}, {'summary': ' '}, {'summary': 'x'*8001}, {'artifact_id': '../x'}, {'analysis_id': '../x'}, {'confidence': 2}])
def test_mcp_invalid_never_reaches_http(override):
    from mcp import Client
    from mcp_server.server import create_server
    def respond(request):
        pytest.fail('Invalid request reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = await client.call_tool('correct_reference_artifact', dict(project_id='work', analysis_id='ref-1', artifact_id='actions', summary='correction', expected_revision=4) | override)
            assert result.is_error
    asyncio.run(verify())

def test_mcp_requires_revision():
    from mcp import Client
    from mcp_server.server import create_server
    def respond(request):
        pytest.fail('Missing revision reached HTTP')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = await client.call_tool('correct_reference_artifact', dict(project_id='work', analysis_id='ref-1', artifact_id='actions', summary='correction'))
            assert result.is_error
    asyncio.run(verify())
