import asyncio
import json
import threading

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server
from backend import app as backend
from test_production_presets import preset_client, add_shot

REAL_THREAD = threading.Thread


def test_create_forwards_nonselecting_request_without_contract_probe():
    writes = []
    def respond(request):
        assert request.method == 'POST' and request.url.path == '/api/projects/create'
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={'project': {'id': 'requested-2'}, 'created': True})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = (await client.call_tool('create_project', {'series': '系列', 'title': '新集', 'project_id': 'requested'})).structured_content
            assert result['data']['project']['id'] == 'requested-2'
    asyncio.run(verify())
    assert writes == [{'series': '系列', 'title': '新集', 'project_id': 'requested', 'select': False}]


def test_create_and_continue_draft_against_real_backend(preset_client, monkeypatch):
    http, workspace = preset_client
    add_shot(http, workspace, 'REF01')
    before = http.get('/api/project').json()
    default = backend.load_project_catalog()['default_project_id']
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            created = (await client.call_tool('create_project', {
                'series': 'Agent 验收', 'title': '新故事', 'project_id': 'agent-story',
            })).structured_content
            assert created['ok'], created
            project_id = created['data']['project']['id']
            assert project_id == 'agent-story'
            catalog = (await client.call_tool('list_production_presets', {})).structured_content
            assert catalog['ok'], catalog
            configured = (await client.call_tool('configure_production_preset', {'project_id': project_id, 'preset_id': 'h3-guided'})).structured_content
            assert configured['ok'], configured
            assert not configured['data']['already_installed']
            repeated = (await client.call_tool('configure_production_preset', {'project_id': project_id, 'preset_id': 'h3-guided'})).structured_content
            assert repeated['data']['already_installed']
            assert repeated['data']['project']['production_preset'] == configured['data']['project']['production_preset']
            values = {'creation_mode': 'original', 'intent': '雨夜重逢', 'character': '红围巾'}
            saved = (await client.call_tool('save_creative_draft', {
                'project_id': project_id, 'expected_revision': 0, 'values': values,
            })).structured_content
            assert saved['ok'], saved
            state = (await client.call_tool('read_project', {'project_id': project_id, 'section': 'state'})).structured_content
            assert state['data']['creative']['values'] == values
            assert state['data']['creative']['revision'] == 1
            assert state['data']['creative']['status'] == 'draft'
            assert not state['data']['reviews']
            plan = (await client.call_tool('read_project', {'project_id': project_id, 'section': 'plan'})).structured_content['data']
            arguments = {'project_id': project_id, 'segment_id': 'S01', 'expected_revision': plan.get('revision', 0), 'duration_seconds': 5, 'prompt': '雨夜相遇'}
            shot = (await client.call_tool('create_shot', arguments)).structured_content
            assert shot['ok'], shot
            assert shot['data']['segment']['prompt'] == '雨夜相遇'
            duplicate = (await client.call_tool('create_shot', arguments)).structured_content
            assert duplicate['error']['http_status'] == 409
            latest = (await client.call_tool('read_project', {'project_id': project_id, 'section': 'plan'})).structured_content['data']
            assert len(latest['segments']) == 1
            source = (await client.call_tool('read_project', {'project_id': 'preset-test', 'section': 'reusable-materials'})).structured_content['data']
            copied = (await client.call_tool('reuse_materials', {
                'project_id': project_id, 'source_project_id': 'preset-test',
                'material_ids': [item['id'] for item in source['materials']],
            })).structured_content
            assert copied['ok'], copied
            materials = (await client.call_tool('read_project', {'project_id': project_id, 'section': 'reusable-materials'})).structured_content['data']['materials']
            image = next(item['path'] for item in materials if item['kind'] == 'image')
            audio = next(item['path'] for item in materials if item['kind'] == 'audio')
            assert 'agent-story' in image and 'agent-story' in audio
            bound = (await client.call_tool('update_shot', {
                'project_id': project_id, 'asset_id': 'S01', 'expected_revision': latest['revision'],
                'changes': {'first_frame': image, 'last_frame': image, 'audio_guide': audio},
            })).structured_content
            assert bound['ok'], bound
            preflight = (await client.call_tool('preflight', {
                'project_id': project_id, 'stage_id': 'motion-generation', 'asset_id': 'S01', 'values': {},
            })).structured_content
            assert preflight['ok'], preflight
            assert preflight['data']['valid']
            assert preflight['data']['materials']['first-frame'] == image
            assert preflight['data']['materials']['guide'] == audio
            current = (await client.call_tool('read_project', {'project_id': project_id, 'section': 'plan'})).structured_content['data']
            ready = (await client.call_tool('create_shot', {
                'project_id': project_id, 'segment_id': 'S02', 'expected_revision': current['revision'],
                'duration_seconds': 5, 'prompt': '庭院挥手',
                'materials': {'first_frame': image, 'last_frame': image, 'audio_guide': audio, 'delivery_master': audio},
            })).structured_content
            assert ready['ok'], ready
            assert ready['data']['segment']['audio'] == {'guide': audio, 'delivery_master': audio}
            direct = (await client.call_tool('preflight', {'project_id': project_id, 'stage_id': 'motion-generation', 'asset_id': 'S02', 'values': {}})).structured_content
            assert direct['ok'] and direct['data']['valid'], direct
    asyncio.run(verify())
    assert http.get('/api/project').json() == before
    assert backend.load_project_catalog()['default_project_id'] == default
    assert http.get('/api/projects/agent-story/tasks').json()['tasks'] == []
