import asyncio
import json

import httpx
from mcp.client import Client

from mcp_server.server import create_server


def test_audio_edit_uses_project_asset_ids_and_preserves_request_key():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={'id': 'edit-1', 'status': 'submitting'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = await client.call_tool('submit_audio_edit', {
                'project_id': 'my-work', 'source_asset_id': 'speech-1',
                'background_asset_id': 'music-1', 'idempotency_key': 'saved-key',
                'duration_seconds': 3.5, 'gain': .8, 'background_offset_seconds': 1,
            })
            assert result.structured_content == {'ok': True, 'data': {'id': 'edit-1', 'status': 'submitting'}}
    asyncio.run(verify())
    assert len(calls) == 1
    assert calls[0].url.path == '/api/projects/my-work/audio-edit-tasks'
    body = json.loads(calls[0].content)
    assert body['source_asset_id'] == 'speech-1'
    assert body['background_asset_id'] == 'music-1'
    assert body['idempotency_key'] == 'saved-key'
    assert body['gain'] == .8 and body['duration_seconds'] == 3.5
    assert body['background_offset_seconds'] == 1


def test_audio_edit_unknown_response_never_retries():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('receipt lost', request=request)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = await client.call_tool('submit_audio_edit', {
                'project_id': 'work', 'source_asset_id': 'speech', 'idempotency_key': 'original',
            })
            assert result.structured_content['error']['outcome_unknown']
    asyncio.run(verify())
    assert len(calls) == 1
