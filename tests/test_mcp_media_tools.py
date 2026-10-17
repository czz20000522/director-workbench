import asyncio
import json
import httpx
from mcp.client import Client
from mcp_server.server import create_server


def test_media_tools_use_single_native_endpoint_with_exclusive_parameters():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={'id': 'media-1', 'status': 'queued'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            await client.call_tool('get_media_capability', {})
            for tool, args in [
                ('create_silence', {'duration_seconds': 5}),
                ('inspect_video', {'source_asset_id': 'video-1', 'sample_count': 6}),
                ('transcribe_audio', {'source_asset_id': 'audio-1', 'expected_text': '天黑了。', 'word_timestamps': False}),
                ('export_silent_video', {'source_asset_id': 'video-1'}),
            ]:
                result = await client.call_tool(tool, {'project_id': 'work', 'idempotency_key': 'saved-' + tool, **args})
                assert result.structured_content['ok']
    asyncio.run(verify())
    assert calls[0].url.path == '/api/media-capability'
    expected = [
        {'operation', 'idempotency_key', 'duration_seconds', 'sample_rate', 'channels'},
        {'operation', 'idempotency_key', 'source_asset_id', 'sample_count'},
        {'operation', 'idempotency_key', 'source_asset_id', 'expected_text', 'language', 'word_timestamps'},
        {'operation', 'idempotency_key', 'source_asset_id'},
    ]
    for request, keys in zip(calls[1:], expected):
        assert request.url.path == '/api/projects/work/media-tasks'
        assert set(json.loads(request.content)) == keys
    assert json.loads(calls[3].content)['expected_text'] == '天黑了。'
    assert json.loads(calls[3].content)['word_timestamps'] is False


def test_media_unknown_response_retains_single_submission():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('lost receipt', request=request)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(handler))) as client:
            result = await client.call_tool('create_silence', {'project_id': 'work', 'idempotency_key': 'keep', 'duration_seconds': 5})
            assert result.structured_content['error']['outcome_unknown']
    asyncio.run(verify())
    assert len(calls) == 1
    assert json.loads(calls[0].content)['idempotency_key'] == 'keep'
