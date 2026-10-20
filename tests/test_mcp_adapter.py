import asyncio
import hashlib
import json

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server


MCP_TOOL_NAMES = """list_projects,upload_material,read_guide_settings,update_guide_settings,read_queue,list_production_presets,configure_production_preset,create_project,read_project,read_script,save_script,list_tasks,get_task,list_shot_versions,restore_shot_version,preflight,repair_shot_recipe_identity,save_creative_draft,record_review,approve_candidate,update_shot,arrange_shot,create_shot,reuse_materials,import_reference_from_material,import_reference_content,import_reference,extract_reference_range,extract_reference_audio,sample_reference_frames,transcribe_reference,list_references,read_reference,start_reference_analysis,reconcile_reference_analysis,annotate_reference_timeline,correct_reference_artifact,correct_reference_classification,convert_reference_to_shots,list_models,submit_shot,submit_batch,control_batch,get_keyframe_capability,submit_assembly,read_assembly_edit,review_assembly_join,set_assembly_sound,preflight_assembly_edit,check_assembly_continuity,submit_keyframe,get_speech_capability,submit_speech,get_submission_receipt,reconcile_generation,resolve_missing_execution,stop_generation,resume_generation,list_media,get_media_url,download_media,submit_audio_edit,submit_audio_master,get_media_capability,create_silence,inspect_video,transcribe_audio,export_silent_video,plan_resume,analyze_audio,normalize_audio,get_upscale_capability,submit_upscale,get_refinement_capability,submit_refinement,list_resource_packs,create_resource_pack,get_resource_pack,add_resource_pack_asset,publish_resource_pack,list_project_resource_packs,get_segment_resources,bind_segment_resources,use_segment_resource_frame,get_task_recovery_plan,repair_task_owner,resolve_ownerless_task,get_continuity_capability,carry_frame,list_audio_assets,extract_audio,start_multiref_task,get_multiref_task""".split(',')
MCP_TOOL_SCHEMA_SHA256 = '6bffc1e4db5411ddff0c92010f71bee34b53dfbf7db5424033703d796eb4133f'


def test_create_shot_forwards_explicit_materials_without_contract_probe():
    writes = []
    def respond(request):
        assert request.method == 'POST' and request.url.path == '/api/projects/a/plan/segments'
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={'segment': {'id': 'S01'}})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = (await client.call_tool('create_shot', {
                'project_id': 'a', 'segment_id': 'S01', 'expected_revision': 2,
                'duration_seconds': 5, 'materials': {'first_frame': 'assets/hero.png', 'audio_guide': 'assets/guide.wav'},
            })).structured_content
            assert result['ok']
    asyncio.run(verify())
    assert len(writes) == 1
    assert writes[0]['expected_revision'] == 2
    assert writes[0]['audio_guide'] == 'assets/guide.wav'
    assert writes[0]['first_frame'] == 'assets/hero.png'
    assert 'delivery_master' not in writes[0]


def test_mcp_protocol_discovery_scoped_calls_and_errors():
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/missing'):
            return httpx.Response(404, json={'detail': 'missing project'})
        return httpx.Response(200, json={'path': request.url.path})
    async def verify():
        server = create_server('http://127.0.0.1:4100', httpx.MockTransport(respond))
        async with Client(server) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == set(MCP_TOOL_NAMES)
            result = await client.call_tool('read_project', {'project_id': 'work-a', 'section': 'state'})
            assert result.structured_content == {'ok': True, 'data': {'path': '/api/projects/work-a/state'}}
            error = await client.call_tool('read_project', {'project_id': 'missing'})
            assert error.structured_content['error']['http_status'] == 404
            await client.call_tool('preflight', {'project_id': 'work-b', 'stage_id': 'motion-generation', 'asset_id': 'S01', 'values': {'prompt': 'hello'}})
        assert requests[-1].method == 'POST'
        assert requests[-1].url.path == '/api/projects/work-b/pipeline/motion-generation/validate'
        assert all('/select' not in request.url.path for request in requests)
    asyncio.run(verify())


def test_mcp_tool_schema_snapshot_is_stable():
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(lambda request: httpx.Response(200, json={})))) as client:
            tools = (await client.list_tools()).tools
            payload = [tool.model_dump(mode='json', exclude_none=True) for tool in tools]
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            assert [tool.name for tool in tools] == MCP_TOOL_NAMES
            assert digest == MCP_TOOL_SCHEMA_SHA256

    asyncio.run(verify())


def test_shot_versions_tools_use_scoped_api_session_and_revision():
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers['authorization'] == 'Bearer local-session'
        assert request.url.path.startswith('/api/projects/work-a/segments/S01/versions')
        if request.method == 'GET':
            return httpx.Response(200, json={'items': [{'task_id': 'task-old', 'generated_at': '2026-09-24T11:12:13.000Z',
                                                       'snapshot': {'prompt': '旧提示词'}, 'is_current': False}],
                                             'total': 2, 'next_offset': None})
        assert json.loads(request.content) == {'expected_plan_revision': 7}
        return httpx.Response(200, json={'revision': 8, 'segment': {'current_version_task_id': 'task-old'}})

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond), session_token='local-session')) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert {'project_id', 'segment_id'} <= set(tools['list_shot_versions'].input_schema['required'])
            assert {'project_id', 'segment_id', 'task_id', 'expected_plan_revision'} <= set(tools['restore_shot_version'].input_schema['required'])
            versions = (await client.call_tool('list_shot_versions', {
                'project_id': 'work-a', 'segment_id': 'S01', 'limit': 1, 'offset': 1,
            })).structured_content
            assert versions['data']['items'][0]['generated_at'] == '2026-09-24T11:12:13.000Z'
            restored = (await client.call_tool('restore_shot_version', {
                'project_id': 'work-a', 'segment_id': 'S01', 'task_id': 'task-old', 'expected_plan_revision': 7,
            })).structured_content
            assert restored['data']['revision'] == 8
        assert [request.method for request in seen] == ['GET', 'POST']
        assert dict(seen[0].url.params) == {'limit': '1', 'offset': '1'}
        assert seen[1].url.path == '/api/projects/work-a/segments/S01/versions/task-old/restore'

    asyncio.run(verify())


def test_shot_version_restore_preserves_conflict_and_unknown_outcome():
    seen = []
    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(409, json={'detail': {'code': 'plan_revision_conflict', 'current_revision': 9}})
        raise httpx.ReadTimeout('receipt lost')

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            args = {'project_id': 'work-a', 'segment_id': 'S01', 'task_id': 'task-old', 'expected_plan_revision': 8}
            conflict = (await client.call_tool('restore_shot_version', args)).structured_content
            assert conflict['error']['detail']['current_revision'] == 9
            unknown = (await client.call_tool('restore_shot_version', args)).structured_content
            assert unknown['error']['outcome_unknown'] and not unknown['error']['retryable']
            assert len(seen) == 2

    asyncio.run(verify())


def test_reference_analysis_receipts_and_unknown_start_are_not_retried():
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        assert request.url.path.startswith('/api/projects/work-b/reverse-analyses/ref-1/')
        if request.url.path.endswith('/semantic-jobs'):
            raise httpx.ReadTimeout('accepted but receipt lost')
        if request.url.path.endswith('/current'):
            return httpx.Response(200, json={'job': {'id': 'job-1', 'status': 'needs_reconcile'}})
        return httpx.Response(409, json={'detail': '尚未发现完整完成回执'})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            args = {'project_id': 'work-b', 'analysis_id': 'ref-1'}
            started = (await client.call_tool('start_reference_analysis', args)).structured_content
            assert started['error']['outcome_unknown']
            assert not started['error']['retryable']
            current = (await client.call_tool('read_reference', {**args, 'section': 'semantic_job'})).structured_content
            assert current['data']['job']['id'] == 'job-1'
            reconciled = (await client.call_tool('reconcile_reference_analysis', args)).structured_content
            assert reconciled['error']['http_status'] == 409
            assert len(seen) == 3
            assert [method for method, _ in seen] == ['POST', 'GET', 'POST']
            assert seen[-1][1].endswith('/semantic-jobs/reconcile')
    asyncio.run(verify())


def test_mcp_rejects_remote_or_credentialed_urls():
    for url in ['https://example.com', 'http://user:pass@localhost:4100', 'http://localhost:4100/other']:
        with pytest.raises(ValueError):
            create_server(url)


def test_write_tool_keeps_version_conflicts_and_unknown_outcomes():
    import json
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        if request.url.path.endswith('/reviews'):
            return httpx.Response(409, json={'detail': {'code': 'record_revision_conflict', 'current_revision': 3}})
        raise httpx.ReadTimeout('lost response')
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            conflict = await client.call_tool('record_review', {'project_id': 'a', 'asset_id': 'S01', 'stage': 'sample', 'expected_revision': 2, 'note': 'check motion'})
            assert conflict.structured_content['error']['detail']['current_revision'] == 3
            assert seen[0]['source'] == 'director-mcp'
            assert seen[0]['expected_revision'] == 2
            unknown = await client.call_tool('save_creative_draft', {'project_id': 'a', 'expected_revision': 0, 'values': {'intent': 'draft'}})
            assert unknown.structured_content['error']['outcome_unknown']
            assert not unknown.structured_content['error']['retryable']
            assert seen[1]['status'] == 'draft'
            assert len(seen) == 2
    asyncio.run(verify())


def test_shot_patch_sends_only_explicit_changes():
    import json
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'revision': 8})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = await client.call_tool('update_shot', {'project_id': 'a', 'asset_id': 'S01', 'expected_revision': 7, 'changes': {'prompt': 'new prompt'}})
            assert result.structured_content['ok']
            assert seen[0].method == 'PATCH'
            assert json.loads(seen[0].content) == {'prompt': 'new prompt', 'expected_revision': 7}
            empty = await client.call_tool('update_shot', {'project_id': 'a', 'asset_id': 'S01', 'expected_revision': 7, 'changes': {}})
            assert empty.structured_content['error']['code'] == 'empty_changes'
            assert len(seen) == 1
    asyncio.run(verify())


def test_reuse_preserves_source_target_and_material_ids():
    import json
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'registered': [], 'already_registered': 1})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            response = await client.call_tool('reuse_materials', {'project_id': 'target', 'source_project_id': 'source', 'material_ids': ['versioned-material']})
            assert response.structured_content['data']['already_registered'] == 1
            assert seen[0].url.path == '/api/projects/target/materials/reuse'
            assert json.loads(seen[0].content) == {'source_project_id': 'source', 'material_ids': ['versioned-material']}
            empty = await client.call_tool('reuse_materials', {'project_id': 'target', 'source_project_id': 'source', 'material_ids': []})
            assert not empty.structured_content['ok']
            assert len(seen) == 1
    asyncio.run(verify())


def test_reference_list_is_compact_and_detail_keeps_provenance():
    artifact = {'summary': 'candidate', 'provenance': 'model_inference', 'user_status': 'pending'}
    def respond(request):
        if request.url.path.endswith('/reverse-analyses'):
            return httpx.Response(200, json={'analyses': [{'id': f'ref-{i}', 'artifacts': {'story': artifact}} for i in range(3)]})
        return httpx.Response(200, json={'artifacts': {'story': artifact}})
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.MockTransport(respond))) as client:
            result = await client.call_tool('list_references', {'project_id': 'a', 'limit': 1, 'offset': 1})
            data = result.structured_content['data']
            assert data['next_offset'] == 2 and data['total'] == 3
            assert data['analyses'][0]['id'] == 'ref-1'
            assert 'artifacts' not in data['analyses'][0]
            detail = await client.call_tool('read_reference', {'project_id': 'a', 'analysis_id': 'ref-1'})
            assert detail.structured_content['data']['artifacts']['story'] == artifact
    asyncio.run(verify())




