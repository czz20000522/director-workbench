"""The remote MCP entry: official wire transport, trusted-host allowlists.

The stdio server in `mcp_server/` is covered by `test_mcp_adapter.py`. This file
only covers the Streamable HTTP mount the workbench serves to external agents:
that each real bearer reaches the existing business routes, that the allow-list
stays fixed, and that the transport refuses hosts and origins that were never
configured. No socket, production database, cloud provider or GPU is used here.
"""
import asyncio
from contextlib import asynccontextmanager

import httpx2
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from backend import app as backend, remote_mcp
from mcp_server.server import create_server
# Used as a callable (.__wrapped__), not only as a pytest fixture, so it stays imported.
from test_private_workbench import private_workbench
from support import seed_test_models


EXPECTED_REMOTE_TOOLS = (
    'list_projects', 'upload_material', 'read_guide_settings', 'read_queue',
    'list_production_presets', 'configure_production_preset', 'create_project',
    'read_project', 'read_script', 'save_script', 'list_tasks', 'get_task',
    'list_shot_versions', 'preflight', 'save_creative_draft', 'record_review',
    'approve_candidate', 'update_shot', 'arrange_shot', 'create_shot',
    'reuse_materials', 'list_references', 'read_reference',
    'start_reference_analysis', 'reconcile_reference_analysis',
    'annotate_reference_timeline', 'correct_reference_artifact',
    'correct_reference_classification', 'convert_reference_to_shots',
    'submit_shot', 'submit_batch', 'get_keyframe_capability', 'submit_assembly',
    'read_assembly_edit', 'review_assembly_join', 'set_assembly_sound',
    'preflight_assembly_edit', 'submit_keyframe', 'get_speech_capability',
    'submit_speech', 'get_submission_receipt', 'reconcile_generation',
    'stop_generation', 'submit_audio_edit', 'submit_audio_master', 'get_media_capability',
    'create_silence', 'inspect_video', 'transcribe_audio', 'export_silent_video',
)
FORBIDDEN_REMOTE_TOOLS = {
    'update_guide_settings', 'import_reference', 'restore_shot_version',
    'repair_shot_recipe_identity', 'control_batch', 'resolve_missing_execution',
    'resume_generation',
}


@pytest.fixture
def access(tmp_path_factory, monkeypatch, request):
    for name in ('DIRECTOR_MCP_ALLOWED_HOSTS', 'DIRECTOR_MCP_ALLOWED_ORIGINS'):
        monkeypatch.delenv(name, raising=False)
    environ = getattr(request, 'param', {})
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    # This bridge invokes the fixture as a plain factory outside pytest's resolver.
    a,b,ownership,root=private_workbench.__wrapped__(tmp_path_factory.mktemp('access'),monkeypatch)
    if 'DIRECTOR_PRIVATE_ORIGINS' in environ:
        backend.app.user_middleware[0].kwargs['origins'] = tuple(environ['DIRECTOR_PRIVATE_ORIGINS'].split(','))
    seed_test_models(backend.SHARED_ROOT)
    monkeypatch.setattr(backend,'queue_task',lambda *args:None)
    monkeypatch.setattr(backend,'require_submission_capacity',lambda:{})
    def fake(path,payload=None,**kwargs):
        if path=='/queue': return {'queue_running':[],'queue_pending':[]}
        if path=='/system_stats': return {'devices':[],'system':{}}
        if path.startswith('/history'): return {}
        raise AssertionError('No real engine access: '+path)
    monkeypatch.setattr(backend,'request_json',fake)
    backend.app.router.routes=[route for route in backend.app.router.routes if getattr(route,'path',None)!='/mcp']
    server=remote_mcp.install(backend.app,http_app_provider=lambda:backend.app,
        token_provider=lambda:backend.current_principal.get().token if backend.current_principal.get() else None)
    mount=next(route for route in backend.app.router.routes if getattr(route,'path',None)=='/mcp')
    backend.app.router.routes.remove(mount)
    backend.app.router.routes.insert(0,mount)
    monkeypatch.setattr(backend,'REMOTE_MCP',server)
    browser=TestClient(backend.app)
    assert browser.post('/api/auth/login',json={'username':'user001','password':'one'}).status_code==200
    browser.headers.update({'Origin':'http://testserver','X-Workspace-User':'user001'})
    return a,b,browser


@asynccontextmanager
async def remote(token, url='http://localhost:4100/mcp/', headers=None):
    # Use the actual official wire transport, not in-memory MCPServer calls.
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=backend.app),
                                headers={'Authorization':token, **(headers or {})},timeout=10) as http:
        async with streamable_http_client(url,http_client=http) as streams:
            async with ClientSession(*streams,read_timeout_seconds=10) as client:
                await client.initialize()
                yield client


def unpack(result):
    return result.structured_content


def test_remote_wire_two_identities_original_business_preflight_submission_receipt(access):
    a,b,_=access
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as ca,remote(b.headers['Authorization']) as cb:
                created=await asyncio.gather(ca.call_tool('create_project',{'series':'A','title':'Alpha','creation_mode':'advanced'}),
                                            cb.call_tool('create_project',{'series':'B','title':'Beta','creation_mode':'advanced'}))
                ids=[unpack(row)['data']['project']['id'] for row in created]
                workflow=a.post(f'/api/projects/{ids[0]}/production-presets/h3-text-native-portrait').json()['project']['production_preset']['workflow']
                listed=await asyncio.gather(ca.call_tool('list_projects',{}),cb.call_tool('list_projects',{}))
                assert [[p['id'] for p in unpack(row)['data']['projects']] for row in listed]==[[ids[0]],[ids[1]]]
                denied=unpack(await cb.call_tool('read_project',{'project_id':ids[0],'section':'plan'}))
                assert denied['error']['http_status']==404
                saved=unpack(await ca.call_tool('create_shot',{'project_id':ids[0],'segment_id':'S01','prompt':'User original text',
                    'duration_seconds':7.3,'expected_revision':0,'workflow':workflow,'generation':{'aspect_ratio':'9:16','seed':42}}))
                assert saved['ok'],saved
                revision=saved['data']['plan']['revision']
                checked=unpack(await ca.call_tool('preflight',{'project_id':ids[0],'asset_id':'S01','stage_id':'motion-text-native-portrait-generation'}))
                assert checked['ok'] and checked['data']['valid'],checked
                assert checked['data']['h3_duration']['frame_count']==175
                args={'project_id':ids[0],'asset_id':'S01','expected_revision':revision,'idempotency_key':'agent-wire-once'}
                first=unpack(await ca.call_tool('submit_shot',args))
                assert first['ok'],first
                assert unpack(await ca.call_tool('submit_shot',args))['data']['id']==first['data']['id']
                receipt=unpack(await ca.call_tool('get_submission_receipt',{'project_id':ids[0],'idempotency_key':'agent-wire-once'}))
                assert receipt['data']['id']==first['data']['id']
                assert len(a.get(f'/api/projects/{ids[0]}/tasks').json()['tasks'])==1
                assert unpack(await cb.call_tool('stop_generation',{'project_id':ids[0],'task_id':first['data']['id']}))['error']['http_status']==404
                stopped=unpack(await ca.call_tool('stop_generation',{'project_id':ids[0],'task_id':first['data']['id']}))
                assert stopped['ok'] and stopped['data']['status']=='stopped'
                illegal=unpack(await ca.call_tool('update_shot',{'project_id':ids[0],'asset_id':'S01','expected_revision':revision,
                    'changes':{'duration_seconds':3.9}}))
                assert illegal['error']['http_status']==422
                stale=unpack(await ca.call_tool('update_shot',{'project_id':ids[0],'asset_id':'S01','expected_revision':0,
                    'changes':{'prompt':'Must not overwrite'}}))
                assert stale['error']['http_status']==409
    asyncio.run(verify())


def test_remote_tool_set_is_the_fixed_allow_list(access):
    a,_,_=access
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                tools=(await client.list_tools()).tools
                assert remote_mcp.REMOTE_TOOLS == EXPECTED_REMOTE_TOOLS
                assert [tool.name for tool in tools] == list(EXPECTED_REMOTE_TOOLS)
                assert all(isinstance(tool.input_schema, dict) and tool.input_schema.get('type') == 'object'
                           for tool in tools)
    asyncio.run(verify())


def test_remote_allowlist_is_backed_by_stdio_schemas():
    async def verify():
        tools = {tool.name: tool for tool in await create_server('http://localhost').list_tools()}
        assert set(EXPECTED_REMOTE_TOOLS) <= tools.keys()
        assert FORBIDDEN_REMOTE_TOOLS.isdisjoint(EXPECTED_REMOTE_TOOLS)
        assert all(isinstance(tools[name].input_schema, dict)
                   and tools[name].input_schema.get('type') == 'object'
                   for name in EXPECTED_REMOTE_TOOLS)

    asyncio.run(verify())


def test_remote_read_only_expansion_uses_real_fixture_routes(access):
    a, _, _ = access
    project = a.post('/api/projects/create', json={
        'series': 'Testing', 'title': 'Read-only MCP expansion',
        'select': False, 'creation_mode': 'advanced',
    }).json()['project']['id']

    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                script = unpack(await client.call_tool('read_script', {'project_id': project}))
                assembly = unpack(await client.call_tool('read_assembly_edit', {'project_id': project}))
                media = unpack(await client.call_tool('get_media_capability', {}))
                assert script['ok'] and assembly['ok'] and media['ok']

    asyncio.run(verify())


def test_remote_unknown_submission_recovers_receipt_and_expired_identity_stops(access,monkeypatch):
    a,_,browser=access
    pid=a.post('/api/projects/create',json={'series':'Testing','title':'Text sample','select':False,'creation_mode':'advanced'}).json()['project']['id']
    workflow=a.post(f'/api/projects/{pid}/production-presets/h3-text-native-portrait').json()['project']['production_preset']['workflow']
    saved=a.post(f'/api/projects/{pid}/plan/segments',json={'prompt':'Original','duration_seconds':5,'expected_revision':0,'workflow':workflow})
    revision=saved.json()['plan']['revision']
    actual_transport=remote_mcp.httpx.ASGITransport
    submissions=[]
    class LoseSubmissionResponse(actual_transport):
        async def handle_async_request(self,request):
            response=await super().handle_async_request(request)
            if request.method=='POST' and request.url.path==f'/api/projects/{pid}/tasks':
                await response.aread()
                assert response.status_code==200, response.text
                submissions.append(request)
                raise httpx.ReadTimeout('fixture lost response after real save',request=request)
            return response
    monkeypatch.setattr(remote_mcp.httpx,'ASGITransport',LoseSubmissionResponse)
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                result=unpack(await client.call_tool('submit_shot',{'project_id':pid,'asset_id':'S01',
                    'expected_revision':revision,'idempotency_key':'wire-unknown-original'}))
                assert not result['ok'] and result['error']['outcome_unknown'] and not result['error']['retryable']
                receipt=unpack(await client.call_tool('get_submission_receipt',{'project_id':pid,
                    'idempotency_key':'wire-unknown-original'}))
                assert receipt['ok'] and receipt['data']['id']
                assert len(submissions)==1 and len(a.get(f'/api/projects/{pid}/tasks').json()['tasks'])==1
                assert a.post('/api/auth/logout').status_code==200
                assert a.get('/api/projects').status_code==401
                # The still-open MCP connection does not cache an authenticated
                # user and cannot execute a follow-up after session revocation.
                with pytest.raises(MCPError):
                    await client.call_tool('create_shot',{'project_id':pid,'segment_id':'S02',
                        'prompt':'Must not save','duration_seconds':5,'expected_revision':revision})
                assert a.post('/mcp/',json={}).status_code==401
                assert len(browser.get(f'/api/projects/{pid}/plan').json()['segments'])==1
                assert len(submissions)==1
    asyncio.run(verify())


@pytest.mark.parametrize('access', [{
    'DIRECTOR_MCP_ALLOWED_HOSTS': 'workbench.example.invalid:4100',
    'DIRECTOR_MCP_ALLOWED_ORIGINS': 'http://workbench.example.invalid:4100',
    'DIRECTOR_PRIVATE_ORIGINS': 'http://testserver,http://workbench.example.invalid:4100',
}], indirect=True)
def test_configured_remote_entry_official_wire_retains_account_boundaries(access):
    a,b,_=access
    url='http://workbench.example.invalid:4100/mcp/'
    origin={'Origin':'http://workbench.example.invalid:4100'}
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization'],url,origin) as own, remote(b.headers['Authorization'],url,origin) as other:
                created=unpack(await own.call_tool('create_project',{'series':'Entry','title':'Isolated','creation_mode':'advanced'}))
                assert created['ok'],created
                pid=created['data']['project']['id']
                assert [row['id'] for row in unpack(await own.call_tool('list_projects',{}))['data']['projects']]==[pid]
                assert unpack(await other.call_tool('list_projects',{}))['data']['projects']==[]
                assert unpack(await other.call_tool('read_project',{'project_id':pid}))['error']['http_status']==404
                async with remote(a.headers['Authorization'],url) as without_origin:
                    assert unpack(await without_origin.call_tool('list_projects',{}))['ok']
                assert a.post('/api/auth/logout').status_code==200
                with pytest.raises(MCPError):
                    await own.call_tool('list_projects',{})
    asyncio.run(verify())


@pytest.mark.parametrize('access', [{
    'DIRECTOR_MCP_ALLOWED_HOSTS': 'workbench.example.invalid:4100',
    'DIRECTOR_MCP_ALLOWED_ORIGINS': 'http://workbench.example.invalid:4100',
    'DIRECTOR_PRIVATE_ORIGINS': 'http://testserver,http://workbench.example.invalid:4100',
}], indirect=True)
def test_configured_entry_rejects_unknown_headers_and_invalid_identity(access):
    a,_,browser=access
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            request={'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}}
            common={'Accept':'application/json, text/event-stream'}
            for host in ('', 'other.example.invalid:4100', 'workbench.example.invalid:4101',
                         'workbench.example.invalid.evil:4100'):
                response=a.post('/mcp/',json=request,headers={**common,'Host':host,
                    'Forwarded':'host=workbench.example.invalid:4100',
                    'X-Forwarded-Host':'workbench.example.invalid:4100'})
                assert response.status_code==421,response.text
            # The private API allows testserver but the MCP origin list does not.
            response=a.post('/mcp/',json=request,headers={**common,'Host':'workbench.example.invalid:4100',
                                                        'Origin':'http://testserver'})
            assert response.status_code==403,response.text
            assert a.post('/mcp/',json=request,headers={**common,'Host':'workbench.example.invalid:4100',
                'Origin':'http://other.example.invalid:4100'}).status_code==403
            # A valid browser Cookie cannot rescue an invalid explicit Bearer.
            assert browser.post('/mcp/',json=request,headers={**common,'Host':'workbench.example.invalid:4100',
                'Origin':'http://workbench.example.invalid:4100','Authorization':'Bearer invalid'}).status_code==401
            assert TestClient(backend.app).post('/mcp/',json=request,headers={**common,
                'Host':'workbench.example.invalid:4100'}).status_code==401
            async with remote(a.headers['Authorization']) as local:
                assert unpack(await local.call_tool('list_projects',{}))['ok']
    asyncio.run(verify())


def test_default_entry_cannot_be_expanded_by_request_headers(access):
    a,_,_=access
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            response=a.post('/mcp/',json={},headers={'Host':'workbench.example.invalid:4100',
                'X-Forwarded-Host':'localhost:4100','Accept':'application/json, text/event-stream'})
            assert response.status_code==421,response.text
    asyncio.run(verify())
