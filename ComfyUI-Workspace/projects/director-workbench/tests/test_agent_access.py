"""No socket, production database, cloud provider or GPU is used here."""
import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager

import httpx2
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from backend import app as backend, agent_access
from test_private_workbench import private_workbench
from test_automatic_creation import seed_test_models


@pytest.fixture
def access(tmp_path_factory, monkeypatch):
    a,b,ownership,root=private_workbench.__wrapped__(tmp_path_factory.mktemp('access'),monkeypatch)
    seed_test_models(backend.ROOT)
    monkeypatch.setattr(backend,'queue_task',lambda *args:None)
    monkeypatch.setattr(backend,'require_submission_capacity',lambda:{})
    def fake(path,payload=None,**kwargs):
        if path=='/queue': return {'queue_running':[],'queue_pending':[]}
        if path=='/system_stats': return {'devices':[],'system':{}}
        if path.startswith('/history'): return {}
        raise AssertionError('No real engine access: '+path)
    monkeypatch.setattr(backend,'request_json',fake)
    backend.app.router.routes=[route for route in backend.app.router.routes if getattr(route,'path',None)!='/mcp']
    server=agent_access.install(backend.app,http_app_provider=lambda:backend.app,
        token_provider=lambda:backend.current_principal.get().token if backend.current_principal.get() else None)
    mount=next(route for route in backend.app.router.routes if getattr(route,'path',None)=='/mcp')
    backend.app.router.routes.remove(mount)
    backend.app.router.routes.insert(0,mount)
    monkeypatch.setattr(backend,'AGENT_REMOTE_MCP',server)
    browser=TestClient(backend.app)
    assert browser.post('/api/auth/login',json={'username':'user001','password':'one'}).status_code==200
    browser.headers.update({'Origin':'http://testserver','X-Workspace-User':'user001'})
    return a,b,browser


@asynccontextmanager
async def remote(token):
    # Use the actual official wire transport, not in-memory MCPServer calls.
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=backend.app),
                                headers={'Authorization':token},timeout=10) as http:
        async with streamable_http_client('http://localhost:4100/mcp/',http_client=http) as streams:
            async with ClientSession(*streams,read_timeout_seconds=10) as client:
                await client.initialize()
                yield client


def unpack(result):
    return result.structured_content


def new_project(a):
    response=a.post('/api/projects/create',json={'series':'Testing','title':'Text sample','select':False,'creation_mode':'auto'})
    assert response.status_code==200,response.text
    return response.json()['project']['id']


def test_discovery_public_minimal_current_schema_and_protected_business(access):
    a,_,_=access
    anonymous=TestClient(backend.app)
    manifest=anonymous.get('/.well-known/director-workbench.json')
    assert manifest.status_code==200 and manifest.json()['mcp']['url']=='/mcp/'
    for path in ('/agent/start','/agent/skill/SKILL.md','/agent/capabilities?group=creation','/agent/capabilities?group=presentation'):
        result=anonymous.get(path)
        assert result.status_code==200,result.text
        assert not any(secret in result.text for secret in ('D:/Comfy','E:/Director','api_key','user001'))
    assert anonymous.get('/api/projects').status_code==401
    assert anonymous.post('/mcp/',json={}).status_code==401
    assert anonymous.get('/openapi.json').status_code==401
    assert a.get('/agent/capabilities?group=unsupported').status_code==404
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client:
                names={tool.name for tool in (await client.list_tools()).tools}
                for group in agent_access.CAPABILITIES:
                    mapping=a.get('/agent/capabilities',params={'group':group}).json()['capabilities']
                    assert all(row['mcp_tool'] in names for row in mapping)
    asyncio.run(verify())


def test_remote_wire_two_identities_original_business_preflight_submission_receipt(access):
    a,b,_=access
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as ca,remote(b.headers['Authorization']) as cb:
                created=await asyncio.gather(ca.call_tool('create_project',{'series':'A','title':'Alpha'}),
                                            cb.call_tool('create_project',{'series':'B','title':'Beta'}))
                ids=[unpack(row)['data']['project']['id'] for row in created]
                listed=await asyncio.gather(ca.call_tool('list_projects',{}),cb.call_tool('list_projects',{}))
                assert [[p['id'] for p in unpack(row)['data']['projects']] for row in listed]==[[ids[0]],[ids[1]]]
                denied=unpack(await cb.call_tool('read_project',{'project_id':ids[0],'section':'plan'}))
                assert denied['error']['http_status']==404
                saved=unpack(await ca.call_tool('create_shot',{'project_id':ids[0],'segment_id':'S01','prompt':'User original text',
                    'duration_seconds':7.3,'expected_revision':0,'generation':{'mode':'auto','aspect_ratio':'9:16','seed':42}}))
                assert saved['ok'],saved
                revision=saved['data']['plan']['revision']
                checked=unpack(await ca.call_tool('preflight',{'project_id':ids[0],'asset_id':'S01'}))
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


def test_browser_binding_ack_not_agent_claim_and_no_business_replay(access):
    a,b,browser=access
    pid=new_project(a)
    tab='tab_isolated_12345678'
    registration={'page_id':tab,'enabled':True}
    assert a.post('/api/agent/pages',json=registration).status_code==403
    page=browser.post('/api/agent/pages',json=registration).json()
    key=page['page_key']
    listing=a.get('/api/agent/pages').json()
    assert key not in json.dumps(listing) and listing['pages'][0]['enabled']
    action={'page_id':tab,'action_id':'draft_once_123','kind':'show_draft','target':{'project_id':pid,'asset_id':'S01','revision':0},
            'values':{'prompt':'Visible original','duration_seconds':5,'generation':{'mode':'auto','aspect_ratio':'16:9','seed':42}}}
    accepted=a.post('/api/agent/page-actions',json=action)
    assert accepted.status_code==200 and accepted.json()['status']=='pending',accepted.text
    assert a.post('/api/agent/page-actions',json=action).json()==accepted.json()
    assert a.get(f'/api/projects/{pid}/plan').json()['segments']==[]
    assert b.post('/api/agent/page-actions',json=action).status_code==404
    assert b.get(f'/api/agent/pages/{tab}/actions').status_code==404
    ack=f'/api/agent/pages/{tab}/actions/draft_once_123/ack'
    body={'status':'presented','message':'Original editor displayed a draft; not saved'}
    assert a.post(ack,json=body,headers={'X-Agent-Page-Key':key}).status_code==403
    assert browser.post(ack,json=body).status_code==403
    assert browser.post(ack,json=body,headers={'X-Agent-Page-Key':key}).status_code==200
    assert a.get(f'/api/agent/pages/{tab}/actions').json()['actions'][0]['status']=='presented'
    assert browser.post(ack,json={'status':'not_presented'},headers={'X-Agent-Page-Key':key}).status_code==409
    bad={**action,'values':{**action['values'],'prompt':'Different'}}
    assert a.post('/api/agent/page-actions',json=bad).status_code==409
    missing={**action,'page_id':None,'action_id':'no_browser_123'}
    assert a.post('/api/agent/page-actions',json=missing).json()['status']=='not_presented'
    assert a.get(f'/api/projects/{pid}/tasks').json()['tasks']==[]


def test_refresh_takeover_lease_revision_expiry_and_session_expiry(access,monkeypatch):
    a,_,browser=access
    pid=new_project(a);tab='tab_lifecycle_123456'
    key=browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True}).json()['page_key']
    action={'page_id':tab,'action_id':'lifecycle_1','kind':'navigate','target':{'project_id':pid,'revision':0},'values':{'page':'shots'}}
    assert a.post('/api/agent/page-actions',json={**action,'target':{'project_id':pid,'revision':9}}).status_code==409
    assert a.post('/api/agent/page-actions',json=action).status_code==200
    page=browser.post('/api/agent/pages',json={'page_id':tab,'enabled':False}).json()
    assert page['page_key']!=key  # Refresh cannot replay or acknowledge former document actions.
    assert a.get(f'/api/agent/pages/{tab}/actions').json()['actions'][0]['status']=='takeover'
    ack=f'/api/agent/pages/{tab}/actions/lifecycle_1/ack'
    assert browser.post(ack,json={'status':'presented'},headers={'X-Agent-Page-Key':key}).status_code==403
    key=page['page_key']
    late=browser.post(ack,json={'status':'presented','message':'late handler'},headers={'X-Agent-Page-Key':key})
    assert late.status_code==200
    assert late.json()['status']=='takeover'
    assert 'acknowledged_at' not in late.json()
    browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True},headers={'X-Agent-Page-Key':key})
    action['action_id']='lifecycle_2'
    assert a.post('/api/agent/page-actions',json=action).status_code==200
    from backend import agent_pages
    original=time.time()
    monkeypatch.setattr(agent_pages.time,'time',lambda:original+65)
    rows=a.get(f'/api/agent/pages/{tab}/actions').json()['actions']
    assert rows[-1]['status']=='not_presented'
    ack=f'/api/agent/pages/{tab}/actions/lifecycle_2/ack'
    assert browser.post(ack,json={'status':'presented'},headers={'X-Agent-Page-Key':key}).status_code==409
    monkeypatch.setattr(agent_pages.time,'time',lambda:original+95)
    assert not a.get('/api/agent/pages').json()['pages'][0]['enabled']
    action['action_id']='lifecycle_3'
    assert a.post('/api/agent/page-actions',json=action).status_code==409
    assert browser.get(f'/api/agent/pages/{tab}/actions',headers={'X-Agent-Page-Key':key}).status_code==200
    assert a.post('/api/agent/page-actions',json=action).status_code==200
    assert a.post('/api/auth/logout').status_code==200
    assert a.get('/api/projects').status_code==401
    assert a.post('/mcp/',json={}).status_code==401


def test_remote_upload_presentation_and_cookie_identity_do_not_bypass_business(access):
    a,b,browser=access
    pid=new_project(a); tab='tab_remote_upload_123'
    page=browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True}).json()
    encoded='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1UAAAAASUVORK5CYII='
    async def verify():
        async with backend.app.router.lifespan_context(backend.app):
            async with remote(a.headers['Authorization']) as client,remote(b.headers['Authorization']) as other:
                uploaded=unpack(await client.call_tool('upload_material',{'project_id':pid,'filename':'start.png','content_base64':encoded}))
                assert uploaded['ok'],uploaded
                assert len(a.get('/api/projects/'+pid).json()['assets'])==1
                denied=unpack(await other.call_tool('upload_material',{'project_id':pid,'filename':'start.png','content_base64':encoded}))
                assert denied['error']['http_status']==404
                pages=unpack(await client.call_tool('list_agent_pages',{}))
                assert pages['data']['pages'][0]['page_id']==tab
                assert page['page_key'] not in json.dumps(pages)
                args={'page_id':tab,'action_id':'remote_visible_123','kind':'show_draft','target':{'project_id':pid,'revision':0},
                      'values':{'prompt':'Original text','duration_seconds':5}}
                action=unpack(await client.call_tool('present_page_action',args))
                assert action['data']['status']=='pending'
                ack=f'/api/agent/pages/{tab}/actions/remote_visible_123/ack'
                assert browser.post(ack,json={'status':'not_presented','message':'Fixture has no rendered App'},
                    headers={'X-Agent-Page-Key':page['page_key']}).status_code==200
                result=unpack(await client.call_tool('read_page_actions',{'page_id':tab}))
                assert result['data']['actions'][0]['status']=='not_presented'
                assert a.get(f'/api/projects/{pid}/plan').json()['segments']==[]
                assert b.get('/api/agent/pages').json()['pages']==[]
    asyncio.run(verify())


def test_page_no_arbitrary_scripts_paths_or_false_targets(access):
    a,_,browser=access
    pid=new_project(a);tab='tab_illegal_input_123'
    browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True})
    action={'page_id':tab,'action_id':'illegal_input_1','kind':'show_draft','target':{'project_id':pid},'values':{'prompt':'safe','duration_seconds':5}}
    for values in ({'script':'alert(1)'},{'url':'https://other'},{'duration_seconds':3.9},{'generation':{'aspect_ratio':'4:3'}}):
        assert a.post('/api/agent/page-actions',json={**action,'values':values}).status_code==422
    assert a.post('/api/agent/page-actions',json={**action,'kind':'play','target':{'project_id':pid,'asset_id':'missing'}}).status_code==404
    assert a.get(f'/api/agent/pages/{tab}/actions').json()['actions']==[]


def test_page_same_action_after_plan_save_returns_original_receipt(access):
    a,_,browser=access
    pid=new_project(a);tab='tab_repeat_saved_123'
    page=browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True}).json()
    action={'page_id':tab,'action_id':'repeat_after_save','kind':'show_draft','target':{'project_id':pid,'revision':0},'values':{'prompt':'Original'}}
    initial=a.post('/api/agent/page-actions',json=action).json()
    assert a.post(f'/api/projects/{pid}/plan/segments',json={'prompt':'Original','duration_seconds':5,'expected_revision':0}).status_code==200
    assert a.post('/api/agent/page-actions',json=action).json()==initial
    assert browser.post(f'/api/agent/pages/{tab}/actions/repeat_after_save/ack',json={'status':'presented'},
        headers={'X-Agent-Page-Key':page['page_key']}).status_code==200
    receipt=a.post('/api/agent/page-actions',json=action).json()
    assert receipt['status']=='presented' and receipt['verification']=='not_attested'


def test_page_expiry_replay_and_inactive_registration_recovery_keep_receipts(access,monkeypatch):
    from backend import agent_pages
    a,_,browser=access
    tab='tab_capacity_123456_00'
    browser.post('/api/agent/pages',json={'page_id':tab,'enabled':True})
    action={'page_id':tab,'action_id':'capacity_receipt','kind':'navigate','values':{'page':'shots'}}
    assert a.post('/api/agent/page-actions',json=action).status_code==200
    for index in range(1,32):
        assert browser.post('/api/agent/pages',json={'page_id':f'tab_capacity_123456_{index:02}','enabled':True}).status_code==200
    new={'page_id':'tab_capacity_123456_32','enabled':True}
    assert browser.post('/api/agent/pages',json=new).status_code==429
    original=time.time();monkeypatch.setattr(agent_pages.time,'time',lambda:original+65)
    read=a.get(f'/api/agent/pages/{tab}/actions').json()['actions'][0]
    assert read['status']=='not_presented'
    assert a.post('/api/agent/page-actions',json=action).json()==read
    monkeypatch.setattr(agent_pages.time,'time',lambda:original+95)
    assert browser.post('/api/agent/pages',json=new).status_code==200
    assert len(a.get('/api/agent/pages').json()['pages'])==32
    assert a.get(f'/api/agent/pages/{tab}/actions').json()['actions'][0]==read


def test_presentation_receipt_explicitly_a_client_report_not_device_attestation(access):
    a,_,_=access
    # Possession of this account's session allows a custom client to emulate a
    # browser Cookie. Origin headers are CSRF checks, not remote attestation.
    client=TestClient(backend.app)
    client.cookies.set('director_session',a.headers['Authorization'].removeprefix('Bearer '))
    client.headers.update({'Origin':'http://testserver','X-Workspace-User':'user001'})
    tab='tab_client_report_123'
    key=client.post('/api/agent/pages',json={'page_id':tab,'enabled':True}).json()['page_key']
    action={'page_id':tab,'action_id':'client_report_123','kind':'navigate','values':{'page':'shots'}}
    assert a.post('/api/agent/page-actions',json=action).status_code==200
    reported=client.post(f'/api/agent/pages/{tab}/actions/client_report_123/ack',json={'status':'presented'},
        headers={'X-Agent-Page-Key':key}).json()
    assert reported['confirmation']=='client_report' and reported['verification']=='not_attested'
    assert not a.get('/.well-known/director-workbench.json').json()['page_confirmation']['attestation']


def test_remote_unknown_submission_recovers_receipt_and_expired_identity_stops(access,monkeypatch):
    a,_,browser=access
    pid=new_project(a)
    saved=a.post(f'/api/projects/{pid}/plan/segments',json={'prompt':'Original','duration_seconds':5,'expected_revision':0})
    revision=saved.json()['plan']['revision']
    actual_transport=agent_access.httpx.ASGITransport
    submissions=[]
    class LoseSubmissionResponse(actual_transport):
        async def handle_async_request(self,request):
            response=await super().handle_async_request(request)
            if request.method=='POST' and request.url.path==f'/api/projects/{pid}/tasks':
                await response.aread()
                assert response.status_code==200
                submissions.append(request)
                raise httpx.ReadTimeout('fixture lost response after real save',request=request)
            return response
    monkeypatch.setattr(agent_access.httpx,'ASGITransport',LoseSubmissionResponse)
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
