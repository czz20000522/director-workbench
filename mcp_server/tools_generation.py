"""Generation, assembly, keyframe, speech, and task-control tools."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

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
        if not asset_ids or any(not isinstance(asset_id, str) or not asset_id.strip() for asset_id in asset_ids):
            raise ValueError('批次必须明确提供至少一个非空分镜 asset_id')
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
                              width: int = 768, height: int = 1344,
                              creative_profile: Literal['general', 'adult-realism', 'adult-anime'] = 'general') -> dict[str, Any]:
        """生成独立图片候选。reference 需参考图片；text 不传图片、可设画幅。成人向配置仅支持虚构成年人和非露骨内容；断线保留原键查回执，不自动采用首尾帧。"""
        return await request('POST', project_path(project_id) + '/keyframe-tasks', json={
            'prompt': prompt, 'reference_image': reference_image, 'idempotency_key': idempotency_key,
            'seed': seed, 'mode': mode, 'width': width, 'height': height, 'creative_profile': creative_profile})

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
