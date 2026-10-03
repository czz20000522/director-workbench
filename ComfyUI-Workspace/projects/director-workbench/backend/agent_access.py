"""Small public discovery plus the official remote MCP adapter on this ASGI app."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
import httpx

from mcp_server.server import create_server
from .mcp_access_config import transport_security

VERSION = '2026-10-03.agent-1'
# One mapping is checked against actual OpenAPI routes and MCP tools. Clients
# request one group rather than loading the entire workbench in context.
CAPABILITIES = {
    'creation': [
        ('创建作品','POST','/api/projects/create','create_project','cpu',False),
        ('列本人作品','GET','/api/projects','list_projects','read',True),
        ('读计划与准备度','GET','/api/projects/{project_id}/state','read_project','read',True),
        ('添加高层分镜','POST','/api/projects/{project_id}/plan/segments','create_shot','cpu',False),
        ('保存合法修改','PATCH','/api/projects/{project_id}/plan/segments/{segment_id}','update_shot','cpu',False),
        ('上传素材','POST','/api/projects/{project_id}/upload','upload_material','cpu',False),
        ('无生成预检','POST','/api/projects/{project_id}/pipeline/{stage_id}/validate','preflight','read',True),
        ('一次生成提交','POST','/api/projects/{project_id}/tasks','submit_shot','gpu',True),
        ('同键原回执','GET','/api/projects/{project_id}/submission-receipt','get_submission_receipt','read',True),
        ('本人队列','GET','/api/queue','read_queue','read',True),
        ('任务与候选','GET','/api/projects/{project_id}/tasks/{task_id}','get_task','read',True),
        ('候选历史','GET','/api/projects/{project_id}/segments/{segment_id}/versions','list_shot_versions','read',True),
        ('停止原任务','POST','/api/projects/{project_id}/tasks/{task_id}/stop','stop_generation','control',True),
    ],
    'presentation': [
        ('发现本人允许引导的页','GET','/api/agent/pages','list_agent_pages','read',True),
        ('受控填草稿或导航播放','POST','/api/agent/page-actions','present_page_action','presentation',True),
        ('实际呈现回执','GET','/api/agent/pages/{page_id}/actions','read_page_actions','read',True),
    ],
}

START = """# 连接你的 Agent

你只需要此工作台的 URL 和自己的账号。先 GET /.well-known/director-workbench.json，再按需 GET /agent/capabilities?group=creation；不用服务器源码、Windows路径、SSH或ComfyUI地址。

1. POST /api/auth/login，JSON仅username/password。保管响应Set-Cookie中的director_session在客户端内存／安全会话容器，不放消息、共享配置或日志。HTTP可用Cookie；写入Cookie方式需同源Origin与X-Workspace-User。非浏览器推荐把该会话作为Authorization: Bearer调用，勿伪造管理员。
2. GET /api/auth/me核对当前身份；GET /api/projects获取本人作品。401重新登录，404不要猜别人作品，409重读比较revision，不自动覆盖。
3. 新作品POST /api/projects/create {series,title,creation_mode:"auto",select:false}，用真实返回project.id。创建／上传／新增分镜不是幂等；未知结果先读取核对，不盲目重发。
4. POST /api/projects/{id}/plan/segments {prompt:"用户原文",duration_seconds:5,generation:{mode:"auto",aspect_ratio:"16:9",seed:42},expected_revision:当前revision}。4–15秒按剧情可改，画幅16:9／1:1／9:16。无图可纯文本；有图须明确绑定其用途。素材来源用上传返回的登记引用，不猜磁盘路径或把库存图当首帧。
5. POST /api/projects/{id}/pipeline/auto/validate {asset_id:"返回分镜ID",values:{}}，读实际帧数／时长／规格和readiness.blockers、allowed_actions、next_actions。预检不生成、不修改计划。GET /openapi.json按需查看完整合法输入。
6. 仅在用户明确生成授权内，POST原/api/projects/{id}/tasks {asset_id,expected_revision:预检revision,idempotency_key:"先保留的唯一键",pipeline_stage_id:"auto"}。等待不换key重发；未知回执GET /submission-receipt?key=原键。通过原任务／队列读真实进度、候选和媒体；任务成功不代表审核／采用／交付。

MCP：同一URL的 /mcp/，官方SDK Streamable HTTP。首批支持可配置Authorization Bearer的客户端；先按上述账号登录获取自己的会话，过期或换账号重新登录。仅支持OAuth且不能配置现有Bearer的宿主暂未支持；这里没有伪造OAuth流程或公开引擎。服务仍走私人网络／现有用户隧道，不需要SSH管理员凭据给Agent。外部直连入口须由维护者配置精确Host/Origin允许列表，默认仍限本机；客户端不能自行扩大允许范围。

部分MCP宿主把会话失效的HTTP401显示为“Server returned an error response”；先用同一身份GET /api/auth/me确认，失效就停止写入并重登，不换生成请求键盲目重发。重新登录仍先查询原任务回执。

页面：用户在“连接你的Agent”明确允许引导当前标签页后，GET /api/agent/pages选择本人目标页，再用受控page-actions显示未保存草稿、定位／导航／候选／播放暂停。页面返回真实呈现回执；没有附属页面就是未呈现，不声称已点击或听到。手工接管／刷新不重放旧动作，GPU等待不抢回用户页面。业务仅调用一次，页面表现不得另发重复生成。

可分发Skill：/agent/skill/SKILL.md。它只描述合法创作方法，细节按需引用发现和OpenAPI。内置云端助手不是外部接入前置。
"""


def capabilities(app, group):
    if group not in CAPABILITIES: raise HTTPException(404,'能力分组不存在')
    paths = app.openapi()['paths']
    rows=[]
    for title,method,path,tool,resource,idempotent in CAPABILITIES[group]:
        operation=paths.get(path,{}).get(method.lower())
        if operation is None: raise RuntimeError(f'Discovery mapping missing route: {method} {path}')
        rows.append({'title':title,'http':{'method':method,'path':path,'operation_id':operation.get('operationId')},
                     'mcp_tool':tool,'resource':resource,'idempotent':idempotent,
                     'schema':{'openapi':'/openapi.json','operation_id':operation.get('operationId')},
                     'unknown_outcome':'查询原回执／保存结果，不盲目重发写入',
                     'version_conflict':'重读revision并比较，不自动升级版本重发'})
    return {'service_version':VERSION,'group':group,'capabilities':rows,'other_groups':list(CAPABILITIES)}


async def prepare_remote(remote):
    # The first remote entry is a small, tested set. Other published operations
    # remain available through authenticated HTTP, not invented alternate tools.
    exposed={row[3] for rows in CAPABILITIES.values() for row in rows}
    exposed.add('read_guide_settings')
    tools=await remote.list_tools()
    names={tool.name for tool in tools}
    if not exposed<=names: raise RuntimeError('Remote MCP discovery does not match the shared capability mapping')
    for tool in tools:
        if tool.name not in exposed: remote.remove_tool(tool.name)


def install(app, *, http_app_provider, token_provider, environ=None):
    remote = create_server('http://localhost', httpx.ASGITransport(app=http_app_provider()),
                           session_token_provider=token_provider,
                           transport_provider=lambda: httpx.ASGITransport(app=http_app_provider()))
    mcp_app=remote.streamable_http_app(streamable_http_path='/',stateless_http=True,json_response=True,
                                     max_request_body_size=24*1024*1024,transport_security=transport_security(environ))
    app.mount('/mcp',mcp_app,name='director-mcp')
    app.state.agent_mcp = remote

    @app.on_event('startup')
    async def start_mcp():
        await prepare_remote(remote)
        context=remote.session_manager.run()
        await context.__aenter__()
        app.state.agent_mcp_context=context

    @app.on_event('shutdown')
    async def stop_mcp():
        context=getattr(app.state,'agent_mcp_context',None)
        if context is not None: await context.__aexit__(None,None,None)

    @app.get('/.well-known/director-workbench.json')
    def discovery():
        return {'name':'Director Workbench','service_version':VERSION,'start':'/agent/start','openapi':'/openapi.json',
                'authentication':{'login':'/api/auth/login','identity':'/api/auth/me','logout':'/api/auth/logout',
                                  'cookie':'director_session','mcp':'Bearer workbench session','oauth':False},
                'mcp':{'url':'/mcp/','transport':'streamable-http','stateless':True,'sdk':'mcp 2.2.0',
                       'supported_clients':'可配置Bearer的Streamable HTTP客户端'},
                'capabilities':'/agent/capabilities?group=creation','groups':list(CAPABILITIES),
                'skill':'/agent/skill/SKILL.md','page_presentation':'/api/agent/pages',
                'page_confirmation':{'source':'client_report','attestation':False,'user_watched_or_heard':False},
                'limits':['业务需本人登录','不公开ComfyUI','页面表现须绑定本人允许的标签页','不存在无回执的审核采用或交付']}

    @app.get('/agent/start',response_class=PlainTextResponse)
    def start(): return START

    @app.get('/agent/capabilities')
    def capability_group(group:str='creation'): return capabilities(app,group)

    @app.get('/agent/skill/SKILL.md',response_class=PlainTextResponse)
    def skill():
        return (Path(__file__).resolve().parents[1]/'.agents/skills/director-workbench/SKILL.md').read_text(encoding='utf-8-sig')
    return remote
