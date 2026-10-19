"""Issue #121: one MCP error/pagination/idempotency contract.

These tests pin the adapter-side contract only: no backend dataset, GPU, socket or
second process is used.  Business routes are replaced by MockTransport responses;
the doc tests read the published contract files.
"""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

pytest.importorskip('mcp')
from mcp import Client

from backend import remote_mcp
from mcp_server import errors
from mcp_server.server import create_server
from test_mcp_adapter import MCP_TOOL_NAMES

PROJECT_ROOT = Path(__file__).resolve().parents[1]

IDEMPOTENT_WRITES = {
    'submit_shot', 'submit_batch', 'submit_assembly', 'submit_keyframe', 'submit_speech',
    'submit_audio_edit', 'submit_audio_master', 'create_silence', 'inspect_video',
    'transcribe_audio', 'export_silent_video', 'import_reference_from_material',
    'reconcile_generation', 'reconcile_reference_analysis',
}
NON_IDEMPOTENT_WRITES = {
    'upload_material', 'create_project', 'create_shot', 'update_shot', 'arrange_shot',
    'save_script', 'save_creative_draft', 'record_review', 'approve_candidate',
    'configure_production_preset', 'update_guide_settings', 'restore_shot_version',
    'repair_shot_recipe_identity', 'reuse_materials', 'import_reference',
    'import_reference_content', 'extract_reference_range', 'extract_reference_audio',
    'sample_reference_frames', 'transcribe_reference', 'start_reference_analysis',
    'annotate_reference_timeline', 'correct_reference_artifact',
    'correct_reference_classification', 'convert_reference_to_shots',
    'review_assembly_join', 'set_assembly_sound', 'stop_generation',
    'resume_generation', 'control_batch', 'resolve_missing_execution',
}


def run(verify):
    asyncio.run(verify())


def server(handler, **kwargs):
    return create_server('http://localhost:4100', httpx.MockTransport(handler), **kwargs)


def test_business_http_failure_is_the_canonical_structured_error():
    def handler(request):
        assert request.url.path == '/api/projects/work/plan'
        return httpx.Response(409, json={'detail': {'code': 'plan_revision_conflict', 'message': '计划版本已变化', 'retryable': False}})

    async def verify():
        async with Client(server(handler)) as client:
            result = await client.call_tool('read_project', {'project_id': 'work', 'section': 'plan'})
        assert result.is_error is True
        payload = result.structured_content
        assert payload['ok'] is False
        error = payload['error']
        assert error['code'] == 'plan_revision_conflict'
        assert error['message'] == '计划版本已变化'
        assert error['kind'] == 'business'
        assert error['retryable'] is False
        assert error['http_status'] == 409
        assert error['detail']['code'] == 'plan_revision_conflict'
        # Clients may catch the SDK tool error or read the JSON payload; both see one object.
        assert json.loads(result.content[0].text) == payload

    run(verify)


def test_transport_failure_separates_retryable_reads_from_unknown_writes():
    def handler(request):
        raise httpx.ReadTimeout('lost receipt', request=request)

    async def verify():
        async with Client(server(handler)) as client:
            read = await client.call_tool('list_projects', {})
            write = await client.call_tool('create_project', {'series': 'A', 'title': 'B', 'creation_mode': 'advanced'})
        read_error = read.structured_content['error']
        assert read.is_error and read_error['kind'] == 'transport'
        assert read_error['retryable'] is True and 'outcome_unknown' not in read_error
        write_error = write.structured_content['error']
        assert write.is_error and write_error['kind'] == 'outcome_unknown'
        assert write_error['outcome_unknown'] is True and write_error['retryable'] is False

    run(verify)


def test_in_body_argument_failures_share_the_same_structured_failure():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'revision': 8})

    async def verify():
        async with Client(server(handler)) as client:
            empty = await client.call_tool('update_shot', {
                'project_id': 'work', 'asset_id': 'S01', 'expected_revision': 0, 'changes': {}})
            operation = await client.call_tool('arrange_shot', {
                'project_id': 'work', 'asset_id': 'S01', 'expected_revision': 0,
                'operation': 'copy', 'target_id': 'S02'})
            materials = await client.call_tool('reuse_materials', {
                'project_id': 'work', 'source_project_id': 'source', 'material_ids': []})
        for result, code in ((empty, 'empty_changes'), (operation, 'invalid_operation_arguments'),
                             (materials, 'invalid_material_count')):
            assert result.is_error is True, (code, result)
            error = result.structured_content['error']
            assert error['code'] == code
            assert error['kind'] == 'argument' and error['retryable'] is False
            assert error['message']
        assert not requests

    run(verify)


@pytest.mark.parametrize('tool, arguments, field', [
    ('list_tasks', {'project_id': 'work', 'limit': 0}, 'limit'),
    ('list_tasks', {'project_id': 'work', 'limit': 101}, 'limit'),
    ('list_tasks', {'project_id': 'work', 'offset': -1}, 'offset'),
    ('list_shot_versions', {'project_id': 'work', 'segment_id': 'S01', 'limit': 101}, 'limit'),
    ('list_references', {'project_id': 'work', 'offset': -1}, 'offset'),
    ('list_media', {'project_id': 'work', 'limit': 0}, 'limit'),
    ('submit_shot', {'project_id': 'work', 'asset_id': 'S01', 'idempotency_key': '   '}, 'idempotency_key'),
    ('submit_shot', {'project_id': 'work', 'asset_id': 'S01', 'idempotency_key': 'x' * 161}, 'idempotency_key'),
    ('submit_batch', {'project_id': 'work', 'asset_ids': [], 'idempotency_key': 'key'}, 'asset_ids'),
    ('submit_batch', {'project_id': 'work', 'asset_ids': ['S01']}, 'idempotency_key'),
    ('submit_keyframe', {'project_id': 'work', 'prompt': 'p', 'idempotency_key': ''}, 'idempotency_key'),
    ('submit_keyframe', {'project_id': 'work', 'prompt': 'p' * 6001, 'idempotency_key': 'k'}, 'prompt'),
    ('submit_keyframe', {'project_id': 'work', 'prompt': 'p', 'idempotency_key': 'k', 'width': 200}, 'width'),
    ('submit_speech', {'project_id': 'work', 'text': '你好', 'voice_reference': 'voice.wav',
                       'idempotency_key': 'k', 'mode': 'auto'}, 'mode'),
    ('submit_speech', {'project_id': 'work', 'text': 'x' * 2001, 'voice_reference': 'voice.wav',
                       'idempotency_key': 'k'}, 'text'),
    ('get_submission_receipt', {'project_id': 'work', 'idempotency_key': ' '}, 'idempotency_key'),
    ('download_media', {'project_id': 'work', 'media_ref': '   '}, 'media_ref'),
    ('convert_reference_to_shots', {'project_id': 'work', 'analysis_id': 'ref-1', 'expected_revision': -1,
                                    'shots': [{'start_seconds': 0, 'end_seconds': 1, 'fields': {'prompt': 'x'}}]},
     'expected_revision'),
    ('correct_reference_artifact', {'project_id': 'work', 'analysis_id': 'ref-1', 'artifact_id': 'story',
                                    'summary': 'checked', 'expected_revision': -1}, 'expected_revision'),
])
def test_schema_violations_are_tool_errors_before_any_http(tool, arguments, field):
    requests = []

    def handler(request):
        requests.append(request)
        pytest.fail('schema-invalid arguments reached HTTP')

    async def verify():
        async with Client(server(handler)) as client:
            result = await client.call_tool(tool, arguments)
        assert result.is_error is True
        assert result.structured_content is None
        text = result.content[0].text
        assert 'validation error' in text or 'validation errors' in text
        assert field in text

    run(verify)
    assert not requests


def test_pagination_bounds_and_meta_contract():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/reverse-analyses'):
            return httpx.Response(200, json={'analyses': [{'id': f'ref-{index}'} for index in range(3)]})
        return httpx.Response(200, json={'items': [], 'tasks': [], 'total': 0, 'next_offset': None})

    async def verify():
        async with Client(server(handler)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ('list_tasks', 'list_shot_versions', 'list_references', 'list_media'):
                properties = tools[name].input_schema['properties']
                assert properties['limit']['minimum'] == 1 and properties['limit']['maximum'] == 100, name
                assert properties['offset']['minimum'] == 0, name
                assert tools[name].meta == errors.PAGINATION_META, name
            await client.call_tool('list_tasks', {'project_id': 'work', 'limit': 1, 'offset': 0})
            await client.call_tool('list_tasks', {'project_id': 'work', 'limit': 100, 'offset': 7})
            await client.call_tool('list_shot_versions', {'project_id': 'work', 'segment_id': 'S01', 'limit': 100, 'offset': 0})
            await client.call_tool('list_media', {'project_id': 'work', 'limit': 100, 'offset': 0})
            first = (await client.call_tool('list_references', {'project_id': 'work', 'limit': 2, 'offset': 0})).structured_content
            rest = (await client.call_tool('list_references', {'project_id': 'work', 'limit': 2, 'offset': 2})).structured_content
        assert dict(seen[0].url.params) == {'limit': '1', 'offset': '0'}
        assert dict(seen[1].url.params) == {'limit': '100', 'offset': '7'}
        assert first['data']['total'] == 3 and first['data']['next_offset'] == 2
        assert rest['data']['next_offset'] is None

    run(verify)


def test_idempotency_annotations_follow_actual_replay_capability():
    async def verify():
        async with Client(create_server('http://localhost:4100')) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert len(tools) == 67
        assert set(tools) == set(MCP_TOOL_NAMES)
        for name in IDEMPOTENT_WRITES:
            annotations = tools[name].annotations
            assert annotations.read_only_hint is False, name
            assert annotations.idempotent_hint is True, name
        for name in NON_IDEMPOTENT_WRITES:
            annotations = tools[name].annotations
            assert annotations.read_only_hint is False, name
            assert annotations.idempotent_hint is False, name
        for name, tool in tools.items():
            if tool.annotations.read_only_hint:
                assert tool.annotations.idempotent_hint is True, name

    run(verify)


def test_media_failures_are_not_nested_inside_success_data():
    def handler(request):
        if request.url.path == '/api/projects/work/media-refs':
            return httpx.Response(200, json={'refs': []})
        pytest.fail('unexpected route ' + request.url.path)

    async def verify():
        async with Client(server(handler)) as client:
            url = await client.call_tool('get_media_url', {'project_id': 'work', 'media_ref': 'asset:a:A'})
            downloaded = await client.call_tool('download_media', {'project_id': 'work', 'media_ref': 'asset:a:A'})
        for result in (url, downloaded):
            assert result.is_error is True, result
            assert result.structured_content['ok'] is False
            assert result.structured_content['error']['code'] == 'media_ref_unavailable'

    run(verify)


def test_published_docs_use_the_current_tool_names_counts_and_media_contract():
    skill = (PROJECT_ROOT / '.agents/skills/director-workbench/SKILL.md').read_text(encoding='utf-8')
    readme = (PROJECT_ROOT / 'mcp_server/README.md').read_text(encoding='utf-8')
    guide = (PROJECT_ROOT / 'docs/public-api-guide.md').read_text(encoding='utf-8')

    appendix_a = skill.split('## 附录 A')[1].split('## 附录 B')[0]
    reported_remote = set(re.findall(r'`([a-z][a-z0-9_]+)`', appendix_a))
    assert reported_remote == set(remote_mcp.REMOTE_TOOLS)
    assert len(reported_remote) == 60

    appendix_b = skill.split('## 附录 B')[1].split('## 附录 C')[0]
    reported_local = set(re.findall(r'^\| `([a-z][a-z0-9_]+)`', appendix_b, re.MULTILINE))
    assert reported_local == set(MCP_TOOL_NAMES) - set(remote_mcp.REMOTE_TOOLS)
    assert len(reported_local) == 7

    assert '**60**' in readme and '**67**' in readme
    assert '当前 60 个；本机 stdio 全量 67 个' in guide
    for document in (skill, readme):
        for tool in ('list_media', 'get_media_url', 'download_media'):
            assert tool in document, tool
        assert '/api/projects/{project_id}/media/{token}' in document
        assert '没有 MCP 下载工具' not in document
    for topic in ('isError=true', 'idempotent_hint', 'next_offset'):
        assert topic in guide or topic in readme, topic
    for document in (skill, readme, guide):
        assert 'adopt_candidate' not in document
