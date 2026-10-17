import asyncio
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from backend import app as backend
from backend.task_scheduler import TaskScheduler
from mcp_server.server import create_server
from test_reverse_workbench import reverse_client


def test_mcp_import_streams_local_reference_to_backend(reverse_client, tmp_path, monkeypatch):
    _, _, existing = reverse_client
    before = existing.read_bytes()
    source = tmp_path / 'reference clip.wav'
    source.write_bytes(b'reference-audio-fixture')
    calls = []
    def decompose(args, **kwargs):
        calls.append(args)
        uploaded = Path(args[args.index('--source') + 1])
        assert uploaded.read_bytes() == source.read_bytes()
        output = Path(args[args.index('--output') + 1])
        document = {'id': args[args.index('--analysis-id') + 1], 'project_id': 'project-a', 'timeline': []}
        (output / 'analysis.json').write_text(json.dumps(document), encoding='utf-8')
    monkeypatch.setattr(backend, 'run_reference_command', decompose)
    # Admission persists a request; execution is driven only by this isolated
    # scheduler tick with the existing fake decomposition command.
    scheduler = TaskScheduler(lambda: backend.db(), backend.DB_LOCK, backend.GPU_ADMISSION_LOCK,
                              lambda: None, backend.revalidate_queued_task, backend.execute_scheduled_task)
    monkeypatch.setattr(backend, 'TASK_SCHEDULER', scheduler)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('offline import consulted ComfyUI'))
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            result = (await client.call_tool('import_reference', {'project_id': 'project-a', 'file_path': str(source)})).structured_content
            assert result['ok']
            assert result['data']['id'].startswith('ref-')
            assert result['data']['id'] != 'ref-test'
            task_id = result['data']['import_task_id']
            assert result['data']['import_status'] == 'scheduler_waiting'
            assert backend.get_task(task_id)['payload']['kind'] == 'reference_decomposition'
            assert calls == []
            assert scheduler.tick(threaded=False) == [task_id]
            assert len(calls) == 1
            assert backend.get_task(task_id)['status'] == 'succeeded'
            archived = (await client.call_tool('read_reference', {
                'project_id': 'project-a', 'analysis_id': result['data']['id'],
            })).structured_content
            assert archived['ok'] and archived['data']['import_task_id'] == task_id
            assert archived['data']['import_status'] == 'succeeded'
            assert scheduler.tick(threaded=False) == [] and len(calls) == 1
            assert existing.read_bytes() == before
            assert source.read_bytes() == b'reference-audio-fixture'
    asyncio.run(verify())


def test_mcp_import_rejects_invalid_sources_without_http(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(500)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            for path in ['relative.mp4', 'https://example.com/video.mp4', str(tmp_path / 'secret.txt'), str(tmp_path / 'missing.wav')]:
                result = (await client.call_tool('import_reference', {'project_id': 'project-a', 'file_path': path})).structured_content
                assert not result['ok']
            assert not requests
    asyncio.run(verify())


def test_mcp_import_lost_receipt_does_not_resend(tmp_path):
    source = tmp_path / 'reference.mp4'
    source.write_bytes(b'video-fixture')
    requests = []
    def respond(request):
        requests.append(request)
        assert request.extensions['timeout']['read'] == 960
        assert b'video-fixture' in request.content
        raise httpx.ReadTimeout('upload accepted; response lost')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = (await client.call_tool('import_reference', {'project_id': 'project-a', 'file_path': str(source)})).structured_content
            assert result['error']['outcome_unknown']
            assert not result['error']['retryable']
            assert len(requests) == 1
    asyncio.run(verify())


def test_mcp_arranges_shots_with_revision_protection(reverse_client):
    _, _, _ = reverse_client
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == 'arrange_shot')
            assert 'merge_range' in tool.input_schema['properties']['operation']['enum']
            assert 'expected_revision' in tool.input_schema['required']
            async def call(name, args):
                return (await client.call_tool(name, args)).structured_content
            converted = await call('convert_reference_to_shots', {'project_id': 'project-a', 'analysis_id': 'ref-test', 'expected_revision': 0})
            plan = converted['data']['plan']
            asset = plan['segments'][0]['id']
            args = {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': plan['revision'], 'operation': 'split', 'split_at_seconds': 2}
            split = await call('arrange_shot', args)
            assert split['ok'], split
            plan = split['data']['plan']
            assert [s['duration_seconds'] for s in plan['segments']] == [2, 3]
            stale = await call('arrange_shot', args)
            assert stale['error']['http_status'] == 409
            merged = await call('arrange_shot', {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': plan['revision'], 'operation': 'merge', 'target_id': plan['segments'][1]['id']})
            assert merged['ok'], merged
            plan = merged['data']['plan']
            assert len(plan['segments']) == 1
            assert plan['segments'][0]['duration_seconds'] == 5
            copied = await call('arrange_shot', {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': plan['revision'], 'operation': 'copy'})
            assert copied['ok'], copied
            plan = copied['data']['plan']
            assert len(plan['segments']) == 2
            moved = await call('arrange_shot', {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': plan['revision'], 'operation': 'move', 'before_id': '__end__'})
            assert moved['ok'], moved
            assert moved['data']['plan']['segments'][-1]['id'] == asset
            invalid = await call('arrange_shot', {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': 0, 'operation': 'copy', 'target_id': asset})
            assert invalid['error']['code'] == 'invalid_operation_arguments'
            plan = moved['data']['plan']
            copied = await call('arrange_shot', {'project_id': 'project-a', 'asset_id': asset, 'expected_revision': plan['revision'], 'operation': 'copy'})
            plan = copied['data']['plan']
            assert len(plan['segments']) == 3
            range_args = {'project_id': 'project-a', 'asset_id': plan['segments'][0]['id'], 'expected_revision': plan['revision'], 'operation': 'merge_range', 'target_id': plan['segments'][-1]['id']}
            missing_target = await call('arrange_shot', {key: value for key, value in range_args.items() if key != 'target_id'})
            assert missing_target['error']['code'] == 'invalid_operation_arguments'
            merged_range = await call('arrange_shot', range_args)
            assert merged_range['ok'], merged_range
            assert len(merged_range['data']['plan']['segments']) == 1
            assert merged_range['data']['plan']['segments'][0]['duration_seconds'] == 15
            assert merged_range['data']['plan']['revision'] == plan['revision'] + 1
            stale_range = await call('arrange_shot', range_args)
            assert stale_range['error']['http_status'] == 409
    asyncio.run(verify())


def test_mcp_preserves_uncertain_analysis_without_restarting(reverse_client):
    _, _, analysis_path = reverse_client
    before = analysis_path.read_bytes()
    root = backend.analysis_job_root()
    job = backend.background_jobs.create(root, 'project-a', 'ref-test')
    backend.background_jobs.update(root, job, owner='previous-server', status='running')

    async def verify():
        server = create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))
        async with Client(server) as client:
            args = {'project_id': 'project-a', 'analysis_id': 'ref-test'}
            started = (await client.call_tool('start_reference_analysis', args)).structured_content
            assert started['ok']
            assert started['data']['job']['id'] == job['id']
            assert started['data']['job']['status'] == 'needs_reconcile'
            current = (await client.call_tool('read_reference', {**args, 'section': 'semantic_job'})).structured_content
            assert current['data']['job']['id'] == job['id']
            result = (await client.call_tool('reconcile_reference_analysis', args)).structured_content
            assert result['error']['http_status'] == 409
            assert len(backend.background_jobs.records(root)) == 1
            assert analysis_path.read_bytes() == before
    asyncio.run(verify())
