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


def test_mcp_speech_returns_same_task_and_can_lookup_receipt(preset_client, monkeypatch):
    _, project = preset_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'run_speech_task', lambda *args: None)
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True, 'missing': []})
    reference = project / 'voice.wav'
    reference.write_bytes(b'input fixture')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            args = {'project_id': 'preset-test', 'text': '你好', 'voice_reference': str(reference), 'idempotency_key': 'mcp-speech-1', 'gen_seconds': 6, 'seed': 42}
            async def call(name, values):
                return (await client.call_tool(name, values)).structured_content
            capability = await call('get_speech_capability', {})
            assert capability == {'ok': True, 'data': {'available': True, 'missing': []}}
            assert (await call('list_tasks', {'project_id': 'preset-test'}))['data']['tasks'] == []
            first = await call('submit_speech', args)
            assert first['ok'], first
            assert first['data']['payload']['request']['parameters']['gen_seconds'] == 6
            assert first['data']['payload']['request']['parameters']['seed'] == 42
            repeated = await call('submit_speech', args)
            assert repeated['data']['id'] == first['data']['id']
            receipt = await call('get_submission_receipt', {'project_id': 'preset-test', 'idempotency_key': args['idempotency_key']})
            assert receipt['ok'], receipt
            conflict = await call('submit_speech', {**args, 'text': '另一句'})
            assert conflict['error']['http_status'] == 409
            listed = await call('list_tasks', {'project_id': 'preset-test'})
            assert [t['id'] for t in listed['data']['tasks']] == [first['data']['id']]
    asyncio.run(verify())


def test_mcp_design_without_reference_and_rejects_mixed_mode(preset_client, monkeypatch):
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'run_speech_task', lambda *args: None)
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True, 'missing': []})

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            async def call(args):
                return (await client.call_tool('submit_speech', args)).structured_content
            args = {'project_id': 'preset-test', 'text': '天快黑了', 'mode': 'design',
                    'voice_description': '年长男性，低沉沙哑', 'idempotency_key': 'design-mcp', 'seed': 42}
            first = await call(args)
            assert first['ok'], first
            frozen = first['data']['payload']['request']
            assert frozen['mode'] == 'design'
            assert frozen['voice_description'] == args['voice_description']
            assert 'voice_reference' not in frozen
            assert (await call(args))['data']['id'] == first['data']['id']
            conflict = await call({**args, 'voice_description': '年轻男性'})
            assert conflict['error']['http_status'] == 409
            for invalid in ({'voice_reference': 'voice.wav'}, {'voice_description': ' '}, {'mode': 'reference'}):
                rejected = await call({**args, 'idempotency_key': 'invalid-design', **invalid})
                assert rejected['error']['http_status'] == 422, rejected
    asyncio.run(verify())
