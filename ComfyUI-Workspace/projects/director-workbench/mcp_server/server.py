"""Local stdio adapter. All business state remains in the HTTP workbench."""
from __future__ import annotations

import os
import base64
from pathlib import Path
from typing import Annotated, Any, Literal, Callable
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
if __package__:
    from .creation_inputs import GenerationSettings
else:
    from creation_inputs import GenerationSettings


class ShotChanges(BaseModel):
    generation: GenerationSettings | None = None
    reference_asset_id: str | None = Field(default=None, max_length=160)
    model_config = ConfigDict(extra='forbid')
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, strict=True, ge=0)
    prompt: str | None = Field(default=None, max_length=100000)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    location: str | None = Field(default=None, max_length=160)
    shot_size: str | None = Field(default=None, max_length=80)
    camera: str | None = Field(default=None, max_length=160)
    wardrobe: str | None = Field(default=None, max_length=160)
    performance: str | None = Field(default=None, max_length=1000)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)
    dependencies: list[str] | None = Field(default=None, max_length=64)


class ShotMaterials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)


class ScriptScene(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    text: str | None = Field(default=None, max_length=500000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)


def create_server(base_url: str, transport: httpx.AsyncBaseTransport | None = None, *, session_token: str | None = None,
                  session_token_provider: Callable[[], str | None] | None = None,
                  transport_provider: Callable[[], httpx.AsyncBaseTransport] | None = None) -> MCPServer:
    parsed = urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('首版 MCP 仅连接本机 HTTP 工作台地址')
    base_url = base_url.rstrip('/')
    if session_token is not None and (not session_token or any(char.isspace() for char in session_token)):
        raise ValueError('无效的工作台会话凭据')
    session_headers = {'Authorization': 'Bearer ' + session_token} if session_token else {}
    server = MCPServer('director-workbench')
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    write_record = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = session_headers
        if session_token_provider is not None:
            token = session_token_provider()
            if not token:
                return {'ok':False,'error':{'code':'authentication_required','http_status':401,'message':'请重新登录本人工作台账号'}}
            headers = {'Authorization':'Bearer '+token}
        try:
            async with httpx.AsyncClient(base_url=base_url, transport=transport_provider() if transport_provider else transport, timeout=30, trust_env=False, follow_redirects=False, headers=headers) as client:
                response = await client.request(method, path, **kwargs)
            data = response.json()
        except httpx.RequestError:
            return {'ok': False, 'error': {'code': 'workbench_unavailable', 'message': '工作台请求未取得回执；写入请求请先读取最新记录核对，不要直接重发', 'retryable': method == 'GET', 'outcome_unknown': method != 'GET'}}
        except ValueError:
            return {'ok': False, 'error': {'code': 'invalid_response', 'message': '工作台未返回 JSON', 'retryable': False}}
        if not response.is_success:
            return {'ok': False, 'error': {'code': 'workbench_http_error', 'http_status': response.status_code, 'detail': data.get('detail', data), 'retryable': False}}
        return {'ok': True, 'data': data}

    def project_path(project_id: str) -> str:
        if not project_id or project_id in {'.', '..'} or '/' in project_id or '\\' in project_id:
            raise ValueError('必须提供有效的作品 ID')
        return '/api/projects/' + quote(project_id, safe='')

    def path_id(value: str, label: str) -> str:
        if not value or value in {'.', '..'} or '/' in value or chr(92) in value or chr(0) in value:
            raise ValueError(f'必须提供有效{label} ID')
        return quote(value, safe='')

    @server.tool(annotations=read_only)
    async def list_projects() -> dict[str, Any]:
        """列出已登记作品。不会切换当前作品；使用返回的 ID 调用后续工具。"""
        return await request('GET', '/api/projects')

    @server.tool(annotations=write_record)
    async def upload_material(project_id:str, filename:str, content_base64:str) -> dict[str, Any]:
        """上传客户端本地素材的内容，沿原multipart登记，非生成。无需服务器路径。
        单文件base64 decoded最多16MiB；大文件用原HTTP multipart。
        非幂等：响应未知先读作品素材核对，不盲目重传；不会自动绑定为首帧或表演声音。
        """
        if not filename or '/' in filename or chr(92) in filename or len(filename)>160:
            raise ValueError('必须提供不含路径的素材文件名')
        if len(content_base64)>22369624: raise ValueError('MCP素材最多16MiB，请使用HTTP multipart上传较大文件')
        try: content=base64.b64decode(content_base64,validate=True)
        except ValueError as exc: raise ValueError('素材内容必须是有效base64') from exc
        if not content or len(content)>16*1024*1024: raise ValueError('MCP素材须为非空、至多16MiB')
        return await request('POST',project_path(project_id)+'/upload',files=[('files',(filename,content))])

    @server.tool(annotations=read_only)
    async def list_agent_pages() -> dict[str, Any]:
        """列本人明确允许引导的浏览器页，不含页面凭据。没有附属页仍可HTTP/MCP创作。"""
        return await request('GET','/api/agent/pages')

    @server.tool(annotations=write_record)
    async def present_page_action(action_id:str, kind:Literal['show_draft','locate','navigate','open_candidate','play','pause','show_preflight','show_task'],
            page_id:str|None=None, target:dict[str,Any]|None=None, values:dict[str,Any]|None=None) -> dict[str,Any]:
        """向本人允许的目标页发受控表现动作，不执行生成／保存。原业务只调用一次。
        target可含project_id/asset_id/revision；values可含prompt/duration_seconds/generation/page。
        无页返回未呈现；pending仅表示已发送，必须read_page_actions核实实际呈现。
        不自ACK／冒称已播放；用户接管或刷新不重放。相同action_id/参数返回原回执。
        """
        return await request('POST','/api/agent/page-actions',json={'action_id':action_id,'kind':kind,'page_id':page_id,
                            'target':target or {},'values':values or {}})

    @server.tool(annotations=read_only)
    async def read_page_actions(page_id:str, after:int=0) -> dict[str,Any]:
        """读取本人该页动作实际回执；不含浏览器ACK凭据，不改变页面或重放业务。"""
        return await request('GET','/api/agent/pages/'+path_id(page_id,'页面')+'/actions',params={'after':after})

    @server.tool(annotations=read_only)
    async def read_guide_settings(project_id: str | None = None) -> dict[str, Any]:
        """读取当前账号的创作指南、可继续进度和作品准备度；不会创建作品或生成。"""
        return await request('GET', '/api/settings/guide', params={'project_id': project_id} if project_id else {})

    @server.tool(annotations=write_record)
    async def update_guide_settings(expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        """保存指南 status、project_id、route、completed_steps；服务端校验版本和作品归属。标记学习进度不代表作品审核通过。"""
        return await request('PATCH', '/api/settings/guide', json={**changes, 'expected_revision': expected_revision})

    @server.tool(annotations=read_only)
    async def read_queue() -> dict[str, Any]:
        """读取当前账号任务的排位、等待原因与限制；管理员读取整体数量。不会派发或抢占任务。"""
        return await request('GET', '/api/queue')

    @server.tool(annotations=read_only)
    async def list_production_presets() -> dict[str, Any]:
        """列出可配置的制作方案、输入要求及模型就绪情况；不会下载模型或提交生成。"""
        return await request('GET', '/api/production-presets')

    @server.tool(annotations=write_record)
    async def configure_production_preset(project_id: str, preset_id: str) -> dict[str, Any]:
        """将目录中的制作方案配置到目标作品。先 list_production_presets 读取实际 ID；保留当前作品选择，不覆盖已有视频制作步骤，不下载模型或启动生成。"""
        if not preset_id or '/' in preset_id or '\\' in preset_id or preset_id in {'.', '..'}:
            raise ValueError('必须提供有效制作方案 ID')
        return await request('POST', project_path(project_id) + '/production-presets/' + quote(preset_id, safe=''))

    @server.tool(annotations=write_record)
    async def create_project(series: str, title: str, project_id: str | None = None) -> dict[str, Any]:
        """创建空白作品，不切换人的当前作品。使用返回的实际 project.id；同名可能自动加后缀。请求结果不确定时先 list_projects 核对，不自动重发。创建后用 save_creative_draft 保存设定。"""
        if project_id is not None: project_path(project_id)
        return await request('POST', '/api/projects/create', json={'series': series, 'title': title,
            **({'project_id':project_id} if project_id is not None else {}), 'select': False})

    @server.tool(annotations=read_only)
    async def read_project(project_id: str, section: Literal['manifest', 'plan', 'state', 'pipeline', 'reusable-materials', 'creative-template'] = 'manifest') -> dict[str, Any]:
        """读取作品档案、分镜、审核、制作步骤、素材；creative-template 返回可沿用的系列设定及来源版本，不修改作品。"""
        return await request('GET', project_path(project_id) + ('' if section == 'manifest' else '/' + section))

    @server.tool(annotations=read_only)
    async def read_script(project_id: str) -> dict[str, Any]:
        """读取完整剧本、场次与独立剧本 revision；尚未建档为 0。场次是叙事单位，不是生成分镜。"""
        return await request('GET', project_path(project_id) + '/script')

    @server.tool(annotations=write_record)
    async def save_script(
        project_id: str, expected_revision: Annotated[int, Field(strict=True, ge=0)],
        title: Annotated[str, Field(max_length=240)],
        text: Annotated[str, Field(max_length=2000000)],
        scenes: Annotated[list[ScriptScene], Field(max_length=1000)],
        target_duration_seconds: Annotated[float | None, Field(gt=0, le=86400)] = None,
    ) -> dict[str, Any]:
        """完整保存剧本草稿并保留历史，不生成故事或媒体。先 read_script 获取版本，保留已有场次 ID；被分镜引用的场次不能删除。409 后重读比较，写入回执未知时先核对，不自动重试。"""
        if len({scene.id for scene in scenes}) != len(scenes):
            raise ValueError('场次 ID 不得重复')
        return await request('PUT', project_path(project_id) + '/script', json={
            'expected_revision': expected_revision, 'title': title, 'text': text,
            'target_duration_seconds': target_duration_seconds,
            'scenes': [scene.model_dump() for scene in scenes],
        })

    @server.tool(annotations=read_only)
    async def list_tasks(project_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """分页读取指定作品的任务。使用 next_offset 继续；未知状态不等于失败。"""
        return await request('GET', project_path(project_id) + '/tasks', params={'limit': limit, 'offset': offset})

    @server.tool(annotations=read_only)
    async def get_task(project_id: str, task_id: str) -> dict[str, Any]:
        """读取一个任务的状态、冻结输入与结果；任务必须属于指定作品。"""
        if not task_id or '/' in task_id or '\\' in task_id or task_id in {'.', '..'}:
            raise ValueError('必须提供有效任务 ID')
        return await request('GET', project_path(project_id) + '/tasks/' + quote(task_id, safe=''))

    @server.tool(annotations=read_only)
    async def list_shot_versions(
        project_id: str, segment_id: str,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
    ) -> dict[str, Any]:
        """分页读取分镜生成历史及同源inspection/参考差异、实际时长、comparison_sources和change_impact。generated_at为生成完成时间UTC，snapshot_complete=false表示旧任务部分记录；技术探测不等于审核采用，读取不生成。"""
        path = project_path(project_id) + '/segments/' + path_id(segment_id, '分镜') + '/versions'
        return await request('GET', path, params={'limit': limit, 'offset': offset})

    @server.tool(annotations=write_record)
    async def restore_shot_version(
        project_id: str, segment_id: str, task_id: str,
        expected_plan_revision: Annotated[int, Field(strict=True, ge=0)],
    ) -> dict[str, Any]:
        """恢复一个成功生成版本为当前版本。先读取 plan.revision 和 list_shot_versions；409 后重新读取比较，未知响应先核对当前版本，不自动重发。"""
        path = (project_path(project_id) + '/segments/' + path_id(segment_id, '分镜')
                + '/versions/' + path_id(task_id, '任务') + '/restore')
        return await request('POST', path, json={'expected_plan_revision': expected_plan_revision})

    @server.tool(annotations=read_only)
    async def preflight(project_id: str, asset_id: str, stage_id: str = 'auto', values: dict[str, Any] | None = None) -> dict[str, Any]:
        """预检已保存分镜，不生成；默认由服务端按分镜输入选择实际方式，无需安装方案或猜步骤。高级请求可指定stage_id。"""
        if not stage_id or '/' in stage_id or '\\' in stage_id or stage_id in {'.', '..'}:
            raise ValueError('必须提供有效步骤 ID')
        return await request('POST', project_path(project_id) + '/pipeline/' + quote(stage_id, safe='') + '/validate', json={'asset_id': asset_id, 'values': values or {}})

    @server.tool(annotations=write_record)
    async def repair_shot_recipe_identity(project_id: str, asset_id: str, task_id: str,
            expected_revision: Annotated[int, Field(strict=True, ge=0)], dry_run: bool = True) -> dict[str, Any]:
        """诊断修复生成完成覆盖配方身份的缺陷。默认只检查；明确dry_run=false才改身份。
        必须提供当前成功任务和计划revision。服务端核对归属、快照来源、原输入和已登记图，
        保留视频、候选、审核及任务；不是历史版本恢复，不接受自选路径，不提交生成。
        """
        return await request('POST', project_path(project_id) + '/segments/' + quote(asset_id, safe='') + '/recipe-identity/repair',
                             json={'task_id': task_id, 'expected_revision': expected_revision, 'dry_run': dry_run})

    @server.tool(annotations=write_record)
    async def save_creative_draft(project_id: str, expected_revision: int, values: dict[str, Any]) -> dict[str, Any]:
        """保存创作草稿供人或 Agent 接续。先读取 state.creative 的 revision（不存在为 0）；冲突须重新读取比较。不会批准制作。"""
        return await request('POST', project_path(project_id) + '/creative', json={'expected_revision': expected_revision, 'values': values, 'status': 'draft', 'source': 'director-mcp'})

    @server.tool(annotations=write_record)
    async def record_review(project_id: str, asset_id: str, stage: Literal['sample', 'finish', 'final'], expected_revision: int, note: str, status: Literal['pending_review', 'changes_requested'] = 'pending_review', candidate_ref: str | None = None) -> dict[str, Any]:
        """记录候选的初审意见或退回修改，不执行最终采用。版本取 state.reviews[asset_id][stage]，不存在为 0；409 后比较更新，不自动重发。"""
        return await request('POST', project_path(project_id) + '/reviews', json={'asset_id': asset_id, 'stage': stage, 'expected_revision': expected_revision, 'note': note, 'status': status, 'candidate_ref': candidate_ref, 'source': 'director-mcp'})

    @server.tool(annotations=write_record)
    async def approve_candidate(project_id: str, asset_id: str, stage: Literal['sample', 'finish', 'final'],
            candidate_ref: str, expected_review_revision: Annotated[int, Field(strict=True, ge=0)],
            expected_plan_revision: Annotated[int, Field(strict=True, ge=0)], note: str,
            confirm_adoption: StrictBool) -> dict[str, Any]:
        """受用户委托，实际检查候选后显式采用；不表示用户亲自批准，不在生成后自动调用。
        必须明确确认、提供当前候选引用及审核/计划版本。先读取state和plan；409重读比较，未知响应核对审核，不自动提升版本或重试。
        只调用原子候选校验的adoptions接口，不回退reviews。不会自动提交后续生成或装配。
        """
        path = project_path(project_id)
        if not asset_id.strip() or len(asset_id) > 160 or asset_id in {'.', '..'} or '/' in asset_id or '\\' in asset_id:
            raise ValueError('必须提供有效资产 ID')
        if confirm_adoption is not True:
            raise ValueError('必须明确确认采用所检查候选')
        if not candidate_ref.strip() or len(candidate_ref) > 1000 or '\x00' in candidate_ref or '..' in candidate_ref.replace('\\', '/').split('/'):
            raise ValueError('必须提供有效候选引用')
        if not note.strip() or len(note) > 4000:
            raise ValueError('必须填写采用依据，至多4000字符')
        return await request('POST', path + '/adoptions', json={
            'asset_id': asset_id, 'stage': stage, 'candidate_ref': candidate_ref,
            'expected_review_revision': expected_review_revision, 'expected_plan_revision': expected_plan_revision,
            'note': note, 'confirm_adoption': confirm_adoption})

    @server.tool(annotations=write_record)
    async def update_shot(project_id: str, asset_id: str, expected_revision: int, changes: ShotChanges) -> dict[str, Any]:
        """修改已有分镜。版本来自 read_project(section='plan').revision；只提交需要修改的字段。素材路径使用作品返回的登记引用，不猜测路径。关联场次在 changes 传 script_scene_id 和 read_script 的 expected_script_revision；空串解除关联。不提交生成。"""
        if not asset_id or asset_id in {'.', '..'} or '/' in asset_id or '\\' in asset_id:
            raise ValueError('必须提供有效分镜 ID')
        patch = changes.model_dump(exclude_unset=True, exclude_none=True)
        if 'script_scene_id' in patch and 'expected_script_revision' not in patch:
            raise ValueError('关联或解除场次需要 expected_script_revision，请先 read_script')
        if not patch:
            return {'ok': False, 'error': {'code': 'empty_changes', 'message': '至少提供一个分镜修改字段', 'retryable': False}}
        return await request('PATCH', project_path(project_id) + '/plan/segments/' + quote(asset_id, safe=''), json={**patch, 'expected_revision': expected_revision})

    @server.tool(annotations=write_record)
    async def arrange_shot(project_id: str, asset_id: str, expected_revision: int, operation: Literal['copy', 'move', 'split', 'merge', 'merge_range'], target_id: str | None = None, before_id: str | None = None, split_at_seconds: float | None = None) -> dict[str, Any]:
        """整理制作分镜：copy 复制；move 用 before_id 指定插入位置（__end__ 表示末尾）；split 用 split_at_seconds 指定镜内拆分时间；merge 用 target_id 指定相邻分镜；merge_range 将当前分镜至 target_id 的连续范围一次合并（目标必须在当前之后）。版本取 plan.revision，必须提供。操作可能使候选和审核过期，不生成视频；回执不确定先读取计划，禁止盲目重试。"""
        if not asset_id or asset_id in {'.', '..'} or '/' in asset_id or '\\' in asset_id:
            raise ValueError('必须提供有效分镜 ID')
        supplied = {key: value for key, value in {'target_id': target_id, 'before_id': before_id, 'split_at_seconds': split_at_seconds}.items() if value is not None}
        required = {'copy': set(), 'move': {'before_id'}, 'split': {'split_at_seconds'}, 'merge': {'target_id'}, 'merge_range': {'target_id'}}[operation]
        if set(supplied) != required:
            return {'ok': False, 'error': {'code': 'invalid_operation_arguments', 'message': '仅提供当前操作需要的参数：move=before_id，split=split_at_seconds，merge/merge_range=target_id，copy=无', 'retryable': False}}
        return await request('POST', project_path(project_id) + '/plan/segments/' + quote(asset_id, safe='') + '/operate', json={'operation': operation, 'expected_revision': expected_revision, **supplied})

    @server.tool(annotations=write_record)
    async def create_shot(project_id: str, segment_id: str, expected_revision: int, duration_seconds: float, prompt: str = '', performance: str = '', materials: ShotMaterials | None = None,
                          script_scene_id: Annotated[str | None, Field(max_length=160)] = None,
                          expected_script_revision: Annotated[int | None, Field(strict=True, ge=0)] = None,
                          generation: GenerationSettings | None = None) -> dict[str, Any]:
        """在作品末尾添加分镜草案，不生成媒体。先读取 plan.revision；使用返回的实际 segment.id 接续。旧版本冲突时先核对分镜，不能换最新版本盲目重试；首尾帧等可随后 update_shot。关联场次需 script_scene_id 和 read_script 的 expected_script_revision；duration_seconds 单独指定镜头时长，不复制整场时长。"""
        if script_scene_id is not None and expected_script_revision is None:
            raise ValueError('关联场次需要 expected_script_revision，请先 read_script')
        references = materials.model_dump(exclude_unset=True, exclude_none=True) if materials else {}
        if generation is not None:
            references['generation'] = generation.model_dump()
        if script_scene_id is not None:
            references.update(script_scene_id=script_scene_id, expected_script_revision=expected_script_revision)
        return await request('POST', project_path(project_id) + '/plan/segments', json={'segment_id': segment_id, 'expected_revision': expected_revision, 'duration_seconds': duration_seconds, 'prompt': prompt, 'performance': performance, **references})

    @server.tool(annotations=write_record)
    async def reuse_materials(project_id: str, source_project_id: str, material_ids: list[str]) -> dict[str, Any]:
        """把参考作品素材复制并登记到目标作品。先读取来源作品 reusable-materials 获取当前素材 ID。返回 registered 资产记录数组（图片引用为 image_path，音频/视频引用为 sources.A）与 already_registered 数量；已登记时可读目标 manifest 的 reused_from 找到副本。副本不继承审核结论，源素材变化时需重新读取列表。"""
        project_path(source_project_id)
        if not 1 <= len(material_ids) <= 128:
            return {'ok': False, 'error': {'code': 'invalid_material_count', 'message': '每次选择 1 至 128 个素材', 'retryable': False}}
        return await request('POST', project_path(project_id) + '/materials/reuse', json={'source_project_id': source_project_id, 'material_ids': material_ids})

    @server.tool(annotations=write_record)
    async def import_reference(project_id: str, file_path: str) -> dict[str, Any]:
        """导入 MCP 服务所在机器上的音视频文件并提取参考时间线。提供用户指定素材的绝对路径，不接受 URL；文件通过现有上传接口复制到目标作品。提取可能持续数分钟，不运行语义模型或生成视频。不是幂等操作：丢失回执后先 list_references 核对，不能自动重新导入。"""
        endpoint = project_path(project_id) + '/reverse-analyses'
        path = Path(file_path)
        if not path.is_absolute() or path.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.mp3', '.wav', '.flac', '.m4a', '.aac'}:
            return {'ok': False, 'error': {'code': 'invalid_reference_file', 'message': '请提供 MCP 所在机器上音视频文件的绝对路径', 'retryable': False}}
        try:
            with path.open('rb') as source:
                return await request('POST', endpoint, files={'file': (path.name, source, 'application/octet-stream')}, timeout=960)
        except OSError:
            return {'ok': False, 'error': {'code': 'reference_file_unreadable', 'message': '参考文件无法读取；若上传已开始，请先查询参考列表核对', 'retryable': False}}

    @server.tool(annotations=read_only)
    async def list_references(project_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """分页列出作品已有参考拆解摘要；用 read_reference 读取时间线、分析工件或分析任务。不会上传或启动分析。"""
        if not 1 <= limit <= 100 or offset < 0:
            return {'ok': False, 'error': {'code': 'invalid_pagination', 'retryable': False}}
        result = await request('GET', project_path(project_id) + '/reverse-analyses')
        if not result['ok']:
            return result
        analyses = result['data']['analyses']
        rows = [{key: item.get(key) for key in ('id', 'title', 'status', 'created_at')} for item in analyses[offset:offset + limit]]
        return {'ok': True, 'data': {'analyses': rows, 'total': len(analyses), 'next_offset': offset + limit if offset + limit < len(analyses) else None}}

    @server.tool(annotations=read_only)
    async def read_reference(project_id: str, analysis_id: str, section: Literal['analysis', 'semantic_job'] = 'analysis') -> dict[str, Any]:
        """读取参考拆解的事实、模型候选、人工校订及时间线，或查询内容分析回执。保留 provenance/user_status，不把模型候选视为人工确认。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        path = project_path(project_id) + '/reverse-analyses/' + analysis_id
        return await request('GET', path + ('/semantic-jobs/current' if section == 'semantic_job' else ''))

    @server.tool(annotations=write_record)
    async def start_reference_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        """启动已导入参考的后台内容分析，可能使用本机模型资源。返回任务回执而非分析完成结果；用 read_reference(section='semantic_job') 查询。已有活动任务返回原任务；完成后的再次调用会启动新分析，回执不确定时禁止盲目重发。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        return await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/semantic-jobs')

    @server.tool(annotations=write_record)
    async def reconcile_reference_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        """核对参考分析的独立完成回执并恢复原任务，不重新运行分析。仅用于 needs_reconcile；409 表示尚不能确认，保留待核对状态，不能据此重启分析。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        return await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/semantic-jobs/reconcile')

    @server.tool(annotations=write_record)
    async def annotate_reference_timeline(project_id: str, analysis_id: str, segment_id: str, expected_revision: int, note: str) -> dict[str, Any]:
        """保存参考时间段的改编备注，标记待复核。版本来自 read_reference 返回的分析 revision（旧文档为 0）；不会自动确认分析或修改已转换的制作分镜。"""
        for value in (analysis_id, segment_id):
            if not value or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in value):
                raise ValueError('必须提供有效分析和时间段 ID')
        return await request('PATCH', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/timeline/' + segment_id, json={'expected_revision': expected_revision, 'user_note': note, 'status': 'pending_review'})

    @server.tool(annotations=write_record)
    async def correct_reference_artifact(project_id: str, analysis_id: str, artifact_id: str,
            summary: str, expected_revision: int, confidence: float | None = None) -> dict[str, Any]:
        """显式校订单个参考工件，保存此前内容及版本历史；不批准整份分析、不重分析或转换。
        先 read_reference 核实工件 ID 与 revision；409 后重读比较，未知响应先读原工件，不自动重发。
        summary 是完整替换后的校订文本；仅代表调用方明确确认此工件，不代表其他内容已通过。
        """
        for value in (analysis_id, artifact_id):
            if not value or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in value):
                raise ValueError('必须提供有效参考拆解与工件 ID')
        if expected_revision < 0 or not summary.strip() or len(summary) > 8000:
            raise ValueError('需要非负分析版本及非空、至多8000字符的校订内容')
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError('置信度必须在0到1之间')
        body = {'summary': summary, 'user_status': 'reviewed', 'expected_revision': expected_revision}
        if confidence is not None:
            body['confidence'] = confidence
        return await request('PATCH', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/artifacts/' + artifact_id, json=body)

    @server.tool(annotations=write_record)
    async def correct_reference_classification(project_id: str, analysis_id: str,
            content_mode: Literal['narrative', 'music_performance', 'dance', 'showcase', 'mood', 'technical', 'other'],
            reason: str, expected_revision: int) -> dict[str, Any]:
        """显式校订参考内容类型及理由，仅确认分类，不批准整份分析、不重分析或转换制作。先 read_reference 取得 revision；409 后重读比较，不盲目更新版本重发。分类写入 content_mode/content_mode_reason，标记 content_mode_user_status=reviewed；后续模型重分析保留校订，另存 content_mode_model_candidate。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        if expected_revision < 0 or not reason.strip() or len(reason) > 2000:
            raise ValueError('需要非负分析版本及非空、至多2000字符的校订理由')
        return await request('PATCH', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/classification',
                             json={'content_mode': content_mode, 'reason': reason, 'expected_revision': expected_revision})

    @server.tool(annotations=write_record)
    async def convert_reference_to_shots(project_id: str, analysis_id: str, expected_revision: int) -> dict[str, Any]:
        """把当前已保存参考时间线转为制作草案，保留校订备注与来源。先 read_reference 核对内容；已关联的分镜不重复添加或覆盖。不会复制参考图为本作关键帧，不生成媒体，不批准制作。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        return await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/convert', json={'expected_revision': expected_revision})

    @server.tool(annotations=write_record)
    async def submit_shot(project_id: str, asset_id: str, idempotency_key: str, pipeline_stage_id: str | None = None, pipeline_values: dict[str, Any] | None = None, prompt: str | None = None, seed: int | None = None,
                          expected_revision: Annotated[int | None, Field(strict=True, ge=0)] = None) -> dict[str, Any]:
        """提交已配置方案的单段生成，返回任务回执。先预检；每次新操作使用唯一请求标识，网络不确定时保留同一标识和参数核对，勿改键重发。不会自动采用结果。"""
        if not idempotency_key.strip() or len(idempotency_key) > 160:
            raise ValueError('请求标识必须为 1–160 个字符且非空白')
        return await request('POST', project_path(project_id) + '/tasks', json={'asset_id': asset_id, 'idempotency_key': idempotency_key, 'pipeline_stage_id': pipeline_stage_id, 'pipeline_values': pipeline_values or {}, 'prompt': prompt, 'seed': seed,
            **({'expected_revision': expected_revision} if expected_revision is not None else {})})

    @server.tool(annotations=write_record)
    async def submit_batch(project_id: str, asset_ids: list[str], idempotency_key: str,
                           pipeline_stage_id: str | None = None, pipeline_values_by_asset: dict[str, dict[str, Any]] | None = None,
                           expected_revision: Annotated[int | None, Field(strict=True, ge=0)] = None) -> dict[str, Any]:
        """按同一公开批次接口冻结并排队，每个分镜在任务边界参与用户轮换；不会自动采用候选。断线先查询请求回执。"""
        return await request('POST', project_path(project_id) + '/batches', json={'asset_ids': asset_ids,
            'idempotency_key': idempotency_key, 'pipeline_stage_id': pipeline_stage_id,
            'pipeline_values_by_asset': pipeline_values_by_asset or {},
            **({'expected_revision': expected_revision} if expected_revision is not None else {})})

    @server.tool(annotations=write_record)
    async def control_batch(project_id: str, batch_id: str, action: Literal['stop', 'resume']) -> dict[str, Any]:
        """停止或恢复当前作品的批次。未派发任务直接停止；已派发任务等待确认，待核对回执不能重复运行。"""
        return await request('POST', project_path(project_id) + '/batches/' + path_id(batch_id, '批次') + '/' + action)

    @server.tool(annotations=read_only)
    async def get_keyframe_capability(mode: Literal['reference', 'text'] = 'reference') -> dict[str, Any]:
        """按 reference 改图或 text 文字生图检查节点与模型，不启动生成。"""
        return await request('GET', '/api/keyframe-capability', params={'mode': mode})

    @server.tool(annotations=write_record)
    async def submit_assembly(project_id: str, expected_revision: int, idempotency_key: str) -> dict[str, Any]:
        """按已审核候选和计划顺序提交成片装配。要求plan.revision及唯一请求标识；不批准分镜。同键同参数找回原任务，断线先get_submission_receipt查询，不能换键重提。完成后read_project读取成片候选，不代表最终采用。"""
        if expected_revision < 0 or not idempotency_key.strip() or len(idempotency_key) > 160:
            raise ValueError('需要非负计划版本及有效请求标识')
        return await request('POST', project_path(project_id) + '/assembly', json={
            'expected_revision': expected_revision, 'idempotency_key': idempotency_key})

    @server.tool(annotations=read_only)
    async def read_assembly_edit(project_id: str) -> dict[str, Any]:
        """读取当前相邻接点、视频版本绑定、预览时间和全片音轨选择；不提交装配。"""
        return await request('GET', project_path(project_id) + '/assembly/edit')

    @server.tool(annotations=write_record)
    async def review_assembly_join(project_id: str, left_id: str, right_id: str,
                                   expected_revision: Annotated[int, Field(strict=True, ge=0)],
                                   status: Literal['pending', 'approved', 'redo_left', 'redo_right'],
                                   constraint: str = '', note: str = '') -> dict[str, Any]:
        """审核当前相邻两段的视频接点，绑定两侧当前视频版本；更换视频后结论自动过期。先 read_assembly_edit 获取顺序和 revision。"""
        return await request('PUT', project_path(project_id) + '/assembly/joins/' +
                             path_id(left_id, '左分镜') + '/' + path_id(right_id, '右分镜'),
                             json={'expected_revision': expected_revision, 'status': status,
                                   'constraint': constraint, 'note': note})

    @server.tool(annotations=write_record)
    async def set_assembly_sound(project_id: str, expected_revision: Annotated[int, Field(strict=True, ge=0)],
                                 policy: Literal['segment_native', 'complete_master', 'mute'],
                                 audio_asset_id: str | None = None) -> dict[str, Any]:
        """选择当前作品已登记的全片音频母版以替换各段原音，或明确保留片段原音；不生成媒体。"""
        return await request('PUT', project_path(project_id) + '/assembly/sound', json={
            'expected_revision': expected_revision, 'policy': policy, 'audio_asset_id': audio_asset_id})

    @server.tool(annotations=read_only)
    async def preflight_assembly_edit(project_id: str) -> dict[str, Any]:
        """无 GPU 地核对全部相邻接点、当前视频、音轨归属与可读时长；ready 才可考虑提交装配。"""
        return await request('GET', project_path(project_id) + '/assembly/preflight')

    @server.tool(annotations=write_record)
    async def submit_keyframe(project_id: str, prompt: str, idempotency_key: str, reference_image: str | None = None,
                              seed: int = 340921, mode: Literal['reference', 'text'] = 'reference',
                              width: int = 768, height: int = 1344) -> dict[str, Any]:
        """生成独立图片候选。reference 需参考图片；text 不传图片、可设画幅。断线保留原键查回执，不自动采用首尾帧。"""
        return await request('POST', project_path(project_id) + '/keyframe-tasks', json={
            'prompt': prompt, 'reference_image': reference_image, 'idempotency_key': idempotency_key,
            'seed': seed, 'mode': mode, 'width': width, 'height': height})

    @server.tool(annotations=read_only)
    async def get_speech_capability() -> dict[str, Any]:
        """检查本机声音执行器和模型文件，返回 available、missing 和语言；不加载模型或生成声音。文件就绪不等于显卡空闲或音频质量已确认。"""
        return await request('GET', '/api/speech-capability')

    @server.tool(annotations=write_record)
    async def submit_speech(project_id: str, text: str, idempotency_key: str,
                            voice_reference: str | None = None,
                            gen_seconds: float | None = None, seed: int | None = None,
                            mode: str = 'reference', voice_description: str | None = None) -> dict[str, Any]:
        """使用本地 AuK-Flash 生成台词候选。默认mode=reference须提供作品内voice_reference且不传voice_description；mode=design须提供非空voice_description且不传参考，可在无音频的新作品设计声音。请求时长默认4.5秒，范围(0,30]，参考与请求总时长不超过30秒；不保证台词完整。seed默认20260923（uint32）。候选试听后可作后续参考，不自动采用。相同标识和参数返回原任务，历史请求重查勿追加参数。断线先get_submission_receipt核对。"""
        body = {'text': text, 'idempotency_key': idempotency_key}
        if mode != 'reference':
            body['mode'] = mode
        if voice_reference is not None:
            body['voice_reference'] = voice_reference
        if voice_description is not None:
            body['voice_description'] = voice_description
        if gen_seconds is not None:
            body['gen_seconds'] = gen_seconds
        if seed is not None:
            body['seed'] = seed
        return await request('POST', project_path(project_id) + '/speech-tasks', json=body)

    @server.tool(annotations=read_only)
    async def get_submission_receipt(project_id: str, idempotency_key: str) -> dict[str, Any]:
        """按原请求标识查生成任务，无需 ComfyUI 在线，不提交生成。404 只代表尚未落库，不证明原请求失败；保留原键并稍后核对。"""
        return await request('GET', project_path(project_id) + '/submission-receipt', params={'key': idempotency_key})

    @server.tool(annotations=write_record)
    async def reconcile_generation(project_id: str, task_id: str) -> dict[str, Any]:
        """核对待恢复任务：视频查原 ComfyUI 队列/历史，声音及媒体工具查独立完成回执；找回结果到原任务，不创建重试或提交生成。无法确认时保留原状态。"""
        if not task_id or task_id in {'.', '..'} or '/' in task_id or '\\' in task_id:
            raise ValueError('必须提供有效任务 ID')
        return await request('POST', project_path(project_id) + '/tasks/' + quote(task_id, safe='') + '/reconcile')

    @server.tool(annotations=write_record)
    async def resolve_missing_execution(project_id: str, task_id: str,
                                        confirm_execution_ended: StrictBool, note: str) -> dict[str, Any]:
        """确认丢失的旧执行已结束后收尾原任务，不重新生成。用于支持人工收尾的 needs_reconcile 任务；先核实旧执行进程已结束并检查输出，不能仅因等待超时使用。调用方必须明确传 true 并记录核实依据；Comfy任务要求在线空队列且原历史为空，CPU音频编辑核对执行进程身份且没有可恢复完成回执；媒体工具还须确认媒体子进程结束，worker 身份未知或无法验证时禁止 resolve。成功将原任务标记失败，保留回执和审计；不确定响应先 get_task 核对，不创建新任务。"""
        if confirm_execution_ended is not True:
            raise ValueError('必须明确确认旧执行确已结束并已检查输出')
        if not note.strip() or len(note) > 2000:
            raise ValueError('核实依据不能为空且不得超过2000字符')
        if not task_id or task_id in {'.', '..'} or '/' in task_id or '\\' in task_id:
            raise ValueError('必须提供有效任务 ID')
        return await request('POST', project_path(project_id) + '/tasks/' + quote(task_id, safe='') + '/resolve-missing',
                             json={'confirm_execution_ended': True, 'note': note})

    @server.tool(annotations=write_record)
    async def stop_generation(project_id: str, task_id: str) -> dict[str, Any]:
        """请求停止指定作品的原任务，返回服务端实际状态；stop_requested 不代表已停止。继续 get_task 核对终态，待核对任务先 reconcile_generation。未知响应先查询原任务，不自动重发。"""
        if not task_id or task_id in {'.', '..'} or '/' in task_id or '\\' in task_id:
            raise ValueError('必须提供有效任务 ID')
        return await request('POST', project_path(project_id) + '/tasks/' + quote(task_id, safe='') + '/stop')

    @server.tool(annotations=write_record)
    async def resume_generation(project_id: str, task_id: str) -> dict[str, Any]:
        """恢复指定作品的已停止/失败任务，可能创建新尝试，非幂等操作。先 get_task 并核对原执行历史；needs_reconcile 先 reconcile_generation，不因超时恢复。服务端可能返回原成功任务或新的 preparing 任务，后续查询返回的实际 ID。声音/图片及CPU音频编辑/媒体工具当前仅核对原回执，不重新执行；媒体工具已停止时保持原任务停止状态。未知响应先 get_task/list_tasks 检查，不得盲目重发。"""
        if not task_id or task_id in {'.', '..'} or '/' in task_id or '\\' in task_id:
            raise ValueError('必须提供有效任务 ID')
        return await request('POST', project_path(project_id) + '/tasks/' + quote(task_id, safe='') + '/resume')

    @server.tool(annotations=write_record)
    async def submit_audio_edit(project_id: str, source_asset_id: str, idempotency_key: str,
                                background_asset_id: str | None = None, start_seconds: float = 0,
                                duration_seconds: float | None = None, gain: float = 1,
                                fade_in_seconds: float = 0, fade_out_seconds: float = 0,
                                background_gain: float = .25, background_offset_seconds: float = 0,
                                pad_silence: bool = False) -> dict[str, Any]:
        """编辑本作品登记的音频素材，产生独立待试听候选，不覆盖或自动绑定原音轨。
        CPU处理PCM16 WAV或最多120秒MP3；MP3转48kHz双声道，背景须同采样率及声道。
        增益为线性倍数；源不足时仅pad_silence=true才补静音。完整母版替换各段原声，不自动保留对白。
        原请求键和参数不可变；未知响应先get_submission_receipt核对，不换键重复编辑。
        """
        if not source_asset_id.strip() or not idempotency_key.strip():
            raise ValueError('必须提供本作声音素材 ID 与请求标识')
        return await request('POST', project_path(project_id) + '/audio-edit-tasks', json={
            'source_asset_id': source_asset_id, 'background_asset_id': background_asset_id,
            'idempotency_key': idempotency_key, 'start_seconds': start_seconds,
            'duration_seconds': duration_seconds, 'gain': gain,
            'fade_in_seconds': fade_in_seconds, 'fade_out_seconds': fade_out_seconds,
            'background_gain': background_gain, 'background_offset_seconds': background_offset_seconds,
            **({'pad_silence': True} if pad_silence else {}),
        })

    @server.tool(annotations=read_only)
    async def get_media_capability() -> dict[str, Any]:
        """读取静音、视频抽帧/技术检查、台词转写及无声导出能力与缺失资源。"""
        return await request('GET', '/api/media-capability')

    async def media_submit(project_id: str, idempotency_key: str, operation: str, **parameters: Any) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise ValueError('必须保存并提供请求标识')
        return await request('POST', project_path(project_id) + '/media-tasks', json={
            'operation': operation, 'idempotency_key': idempotency_key, **parameters,
        })

    @server.tool(annotations=write_record)
    async def create_silence(project_id: str, idempotency_key: str,
                             duration_seconds: Annotated[float, Field(gt=0, le=120)],
                             sample_rate: Literal[16000, 24000, 48000] = 24000,
                             channels: Literal[1, 2] = 1) -> dict[str, Any]:
        """生成本作静音WAV候选。先保存请求键；未知响应使用get_submission_receipt，不换键重发。"""
        return await media_submit(project_id, idempotency_key, 'silence', duration_seconds=duration_seconds,
                                  sample_rate=sample_rate, channels=channels)

    @server.tool(annotations=write_record)
    async def inspect_video(project_id: str, source_asset_id: str, idempotency_key: str,
                            sample_count: Annotated[int, Field(ge=3, le=60)] = 12) -> dict[str, Any]:
        """对本作视频素材ID执行技术检查和抽帧；报告不是画面/表演人工验收。未知响应核对原请求键。"""
        return await media_submit(project_id, idempotency_key, 'video_qc', source_asset_id=source_asset_id, sample_count=sample_count)

    @server.tool(annotations=write_record)
    async def transcribe_audio(project_id: str, source_asset_id: str, idempotency_key: str,
                               expected_text: Annotated[str, Field(max_length=2000)] = '',
                               language: Literal['zh', 'en', 'auto'] = 'zh',
                               word_timestamps: StrictBool = True) -> dict[str, Any]:
        """转写本作音频或视频，返回时间戳和预期台词差异提示；不能代替试听或批准音色。未知响应核对原请求键。"""
        return await media_submit(project_id, idempotency_key, 'transcribe', source_asset_id=source_asset_id,
                                  expected_text=expected_text, language=language, word_timestamps=word_timestamps)

    @server.tool(annotations=write_record)
    async def export_silent_video(project_id: str, source_asset_id: str, idempotency_key: str) -> dict[str, Any]:
        """导出本作视频的无声审片候选，不覆盖原片。未知响应先get_submission_receipt；后续get_task查看outputs/report。"""
        return await media_submit(project_id, idempotency_key, 'silent_video', source_asset_id=source_asset_id)

    return server


if __name__ == '__main__':
    create_server(os.environ.get('DIRECTOR_WORKBENCH_URL', 'http://127.0.0.1:4100'),
                  session_token=os.environ.get('DIRECTOR_WORKBENCH_SESSION')).run(transport='stdio')
