import asyncio
import threading

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from backend import app as backend
from mcp_server.server import create_server
from test_keyframe_inputs import reference_image
from test_production_presets import preset_client

REAL_THREAD = threading.Thread


def test_mcp_keyframe_admission_receipt_and_environment_change(preset_client, monkeypatch):
    _, project = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'run_workflow_submission', lambda *args: None)
    readiness = {'available': False, 'missing_nodes': ['SaveImage'], 'missing_models': []}
    monkeypatch.setattr(backend, 'keyframe_capability', lambda: dict(readiness))
    # FastAPI captured the original route callable; provide the same registry
    # failure through its actual request boundary for the read-only MCP check.
    monkeypatch.setattr(backend, 'request_json', lambda *args: {})
    reference = reference_image(project)

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            async def call(name, args):
                return (await client.call_tool(name, args)).structured_content
            args = {'project_id': 'preset-test', 'prompt': '雨夜中挥手', 'reference_image': str(reference),
                    'seed': 42, 'idempotency_key': 'mcp-keyframe-proof'}
            capability = await call('get_keyframe_capability', {})
            assert capability['ok'] and not capability['data']['available']
            rejected = await call('submit_keyframe', args)
            assert rejected['error']['http_status'] == 503
            assert (await call('list_tasks', {'project_id': 'preset-test'}))['data']['tasks'] == []
            readiness.update(available=True, missing_nodes=[])
            first = await call('submit_keyframe', args)
            assert first['ok'], first
            task_id = first['data']['id']
            assert first['data']['payload']['request']['seed'] == 42
            readiness.update(available=False, missing_nodes=['SaveImage'])
            repeated = await call('submit_keyframe', args)
            assert repeated['ok'] and repeated['data']['id'] == task_id
            receipt = await call('get_submission_receipt', {'project_id': 'preset-test', 'idempotency_key': args['idempotency_key']})
            assert receipt['ok'] and receipt['data']['id'] == task_id
            conflict = await call('submit_keyframe', {**args, 'seed': 43})
            assert conflict['error']['http_status'] == 409
            assert [task['id'] for task in (await call('list_tasks', {'project_id': 'preset-test'}))['data']['tasks']] == [task_id]
    asyncio.run(verify())


def test_mcp_text_image_without_reference(preset_client, monkeypatch):
    _, project = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'run_workflow_submission', lambda *args: None)
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    monkeypatch.setattr(backend, 'request_json', lambda *args: {})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            async def call(name, args):
                return (await client.call_tool(name, args)).structured_content
            capability = await call('get_keyframe_capability', {'mode': 'text'})
            assert capability['data']['engine'] == 'Krea 2 Turbo'
            assert not capability['data']['requires_reference_image']
            args = {'project_id': 'preset-test', 'mode': 'text', 'prompt': '新的角色', 'idempotency_key': 'text-mcp'}
            first = await call('submit_keyframe', args)
            assert first['ok'], first
            assert first['data']['payload']['request']['mode'] == 'text'
            assert 'reference_image' not in first['data']['payload']['request']
            again = await call('submit_keyframe', args)
            assert again['data']['id'] == first['data']['id']
            conflict = await call('submit_keyframe', {**args, 'width': 1024})
            assert conflict['error']['http_status'] == 409
    asyncio.run(verify())
