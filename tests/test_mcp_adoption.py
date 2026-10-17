import asyncio,json
import httpx
import pytest
pytest.importorskip('mcp')
from mcp import Client
from mcp_server.server import create_server

ARGS=dict(project_id='work',asset_id='S01',stage='sample',candidate_ref='registered/video.mp4',expected_review_revision=0,expected_plan_revision=2,note='Delegated technical selection',confirm_adoption=True)
FIELDS=set(ARGS)-{'project_id'}

@pytest.mark.parametrize('status',[200,409,None])
def test_adoption_protocol_schema_bearer_and_no_retry(status):
 requests=[]
 def respond(r):
  requests.append(r)
  assert r.headers['Authorization']=='Bearer test-session'
  assert r.method=='POST'
  if status is None:raise httpx.ReadTimeout('unknown',request=r)
  return httpx.Response(status,json={'status':'approved','revision':1} if status==200 else {'detail':'stale'})
 async def verify():
  async with Client(create_server('http://localhost',httpx.MockTransport(respond),session_token='test-session')) as c:
   tool=next(t for t in (await c.list_tools()).tools if t.name=='approve_candidate')
   assert set(tool.input_schema['required'])==set(ARGS)
   result=(await c.call_tool(tool.name,ARGS)).structured_content
   assert result['ok']==(status==200)
   if status==409:assert result['error']['http_status']==409
   if status is None:assert result['error']['outcome_unknown'] and not result['error']['retryable']
 asyncio.run(verify())
 assert [r.method for r in requests]==['POST']
 assert requests[0].url.path=='/api/projects/work/adoptions'
 assert json.loads(requests[0].content)=={k:ARGS[k] for k in FIELDS}

@pytest.mark.parametrize('override',[{'confirm_adoption':False},{'confirm_adoption':1},{'expected_plan_revision':-1},{'expected_review_revision':True},{'expected_review_revision':'0'},{'candidate_ref':'../other.mp4'},{'candidate_ref':' '},{'candidate_ref':'x'*1001},{'note':' '},{'stage':'unknown'},{'project_id':'../x'},{'asset_id':'../x'}])
def test_invalid_adoption_never_reaches_http(override):
 def respond(r):pytest.fail('invalid request reached HTTP')
 async def verify():
  async with Client(create_server('http://localhost',httpx.MockTransport(respond))) as c:
   assert (await c.call_tool('approve_candidate',ARGS|override)).is_error
 asyncio.run(verify())

def test_confirmation_required():
 def respond(r):pytest.fail('missing confirmation reached HTTP')
 async def verify():
  async with Client(create_server('http://localhost',httpx.MockTransport(respond))) as c:
   assert (await c.call_tool('approve_candidate',{k:v for k,v in ARGS.items() if k!='confirm_adoption'})).is_error
 asyncio.run(verify())

@pytest.mark.parametrize('status', [401, 404, 422, None])
def test_adoption_http_failure_never_falls_back_to_reviews(status):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == 'POST' and request.url.path == '/api/projects/work/adoptions'
        if status is None:
            raise httpx.ConnectError('offline', request=request)
        return httpx.Response(status, json={'detail': 'declined'})
    async def verify():
        async with Client(create_server('http://localhost', httpx.MockTransport(respond))) as client:
            result = (await client.call_tool('approve_candidate', ARGS)).structured_content
            assert not result['ok'] and not result['error']['retryable']
            if status is not None:
                assert result['error']['http_status'] == status
            else:
                assert result['error']['outcome_unknown']
    asyncio.run(verify())
    assert len(requests) == 1
