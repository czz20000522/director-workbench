"""Creative project, script, shot, and task tools."""
from __future__ import annotations

import base64
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool

if __package__:
    from .creation_inputs import GenerationSettings
    from .models import ScriptScene, ShotChanges, ShotMaterials
else:
    from creation_inputs import GenerationSettings
    from models import ScriptScene, ShotChanges, ShotMaterials

def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

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
