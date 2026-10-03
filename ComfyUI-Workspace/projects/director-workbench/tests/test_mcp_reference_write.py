import asyncio
import json
import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from backend import app as backend
from mcp_server.server import create_server
from test_reverse_workbench import reverse_client


def test_mcp_timeline_write_uses_real_revision_guard(reverse_client):
    _, _, path = reverse_client
    async def verify():
        server = create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))
        async with Client(server) as client:
            arguments = {'project_id': 'project-a', 'analysis_id': 'ref-test', 'segment_id': 'R01', 'expected_revision': 0, 'note': '主角改为回头微笑'}
            saved = await client.call_tool('annotate_reference_timeline', arguments)
            assert saved.structured_content['ok']
            assert saved.structured_content['data']['analysis']['revision'] == 1
            assert saved.structured_content['data']['segment']['status'] == 'pending_review'
            before = path.read_bytes()
            stale = await client.call_tool('annotate_reference_timeline', {**arguments, 'note': '过期意见'})
            assert stale.structured_content['error']['http_status'] == 409
            assert path.read_bytes() == before
            assert json.loads(before)['timeline'][0]['user_note'] == arguments['note']
    asyncio.run(verify())


def test_mcp_conversion_preserves_existing_shot_edits(reverse_client):
    _, _, _ = reverse_client
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            args = {'project_id': 'project-a', 'analysis_id': 'ref-test', 'expected_revision': 0}
            converted = (await client.call_tool('convert_reference_to_shots', args)).structured_content
            assert converted['ok'], converted
            shot = converted['data']['created_segments'][0]
            assert shot['source_range_seconds'] == [0, 5]
            assert shot['keyframes']['first'] is None
            edited = (await client.call_tool('update_shot', {
                'project_id': 'project-a', 'asset_id': shot['id'],
                'expected_revision': converted['data']['plan']['revision'], 'changes': {'performance': '导演的新表演'},
            })).structured_content
            assert edited['ok'], edited
            stale = (await client.call_tool('convert_reference_to_shots', args)).structured_content
            assert stale['error']['http_status'] == 409
            repeated = (await client.call_tool('convert_reference_to_shots', {**args, 'expected_revision': converted['data']['analysis']['revision']})).structured_content
            assert repeated['ok'], repeated
            assert repeated['data']['created_segments'] == []
            assert len(repeated['data']['plan']['segments']) == 1
            assert repeated['data']['plan']['segments'][0]['performance'] == '导演的新表演'
    asyncio.run(verify())
