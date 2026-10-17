import asyncio
import threading

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from backend import app as backend
from mcp_server.server import create_server
from test_production_presets import preset_client

REAL_THREAD = threading.Thread


def test_mcp_series_snapshot_creates_independent_unapproved_draft(preset_client, monkeypatch):
    http, _ = preset_client
    saved = http.post('/api/projects/preset-test/creative', json={
        'expected_revision': 0, 'status': 'approved', 'values': {'world': '海边小镇',
        'character': '穿蓝衣的水豚', 'voice': '轻柔声线', 'intent': '仅来源这集的剧情'}})
    assert saved.status_code == 200
    before = http.get('/api/projects/preset-test/state').json()
    selected = backend.CURRENT_PROJECT_ID
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            async def call(name, args):
                result = (await client.call_tool(name, args)).structured_content
                assert result['ok'], result
                return result['data']
            template = await call('read_project', {'project_id': 'preset-test', 'section': 'creative-template'})
            assert template['source']['revision'] == before['creative']['revision']
            created = await call('create_project', {'series': '系列副本', 'title': '下一集', 'project_id': 'next-episode'})
            target = created['project']['id']
            await call('save_creative_draft', {'project_id': target, 'expected_revision': 0, 'values': template['values']})
            state = await call('read_project', {'project_id': target, 'section': 'state'})
            assert state['creative']['status'] != 'approved'
            assert state['creative']['values']['character'] == '穿蓝衣的水豚'
            assert state['creative']['values']['intent'] == ''
            assert not state['reviews']
            changed = {**state['creative']['values'], 'world': '下一集的新城市'}
            await call('save_creative_draft', {'project_id': target, 'expected_revision': state['creative']['revision'], 'values': changed})
            source = await call('read_project', {'project_id': 'preset-test', 'section': 'state'})
            assert source == before
    asyncio.run(verify())
    assert backend.CURRENT_PROJECT_ID == selected
