"""Reusable resource packs, ownerless recovery, continuity and audio assets."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations,
             idempotent_write: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

    @server.tool(annotations=read_only)
    async def list_resource_packs(series: str) -> dict[str, Any]:
        """列出某系列的资源包事实；不绑定、不生成、不推荐。"""
        return await request('GET', '/api/series/' + path_id(series, '系列') + '/resource-packs')

    @server.tool(annotations=write_record)
    async def create_resource_pack(series: str, kind: Literal['character', 'scene', 'prop', 'sound'],
                                   title: Annotated[str, Field(min_length=1, max_length=200)],
                                   pack_id: Annotated[str | None, Field(max_length=128)] = None) -> dict[str, Any]:
        """显式创建资源包草稿；不自动生成资产、不自动发布。"""
        body: dict[str, Any] = {'kind': kind, 'title': title}
        if pack_id is not None:
            body['id'] = pack_id
        return await request('POST', '/api/series/' + path_id(series, '系列') + '/resource-packs', json=body)

    @server.tool(annotations=read_only)
    async def get_resource_pack(series: str, pack_id: str) -> dict[str, Any]:
        """读取单个资源包及其资产事实。"""
        return await request('GET', '/api/series/' + path_id(series, '系列') + '/resource-packs/' + path_id(pack_id, '资源包'))

    @server.tool(annotations=write_record)
    async def add_resource_pack_asset(series: str, pack_id: str, project_id: str,
                                      role: Annotated[str, Field(max_length=80)] = '',
                                      view: Annotated[str, Field(max_length=80)] = '',
                                      media_ref: Annotated[str | None, Field(max_length=1024)] = None,
                                      asset_id: Annotated[str | None, Field(max_length=128)] = None) -> dict[str, Any]:
        """把既有项目媒体复制登记进资源包草稿；不生成、不发布。"""
        body: dict[str, Any] = {'project_id': project_id, 'role': role, 'view': view}
        if media_ref is not None:
            body['media_ref'] = media_ref
        if asset_id is not None:
            body['asset_id'] = asset_id
        return await request('POST', '/api/series/' + path_id(series, '系列') + '/resource-packs/' + path_id(pack_id, '资源包') + '/assets', json=body)

    @server.tool(annotations=write_record)
    async def publish_resource_pack(series: str, pack_id: str) -> dict[str, Any]:
        """冻结并发布资源包；发布后不可再追加资产。"""
        return await request('POST', '/api/series/' + path_id(series, '系列') + '/resource-packs/' + path_id(pack_id, '资源包') + '/publish')

    @server.tool(annotations=read_only)
    async def list_project_resource_packs(project_id: str) -> dict[str, Any]:
        """列出本项目同系列可用资源包；不选择、不绑定。"""
        return await request('GET', project_path(project_id) + '/resource-packs')

    @server.tool(annotations=read_only)
    async def get_task_recovery_plan() -> dict[str, Any]:
        """读取 ownerless needs_reconcile 恢复计划；管理员/本机只读，不自动处置。"""
        return await request('GET', '/api/admin/tasks/recovery-plan')

    @server.tool(annotations=write_record)
    async def repair_task_owner(task_id: str, confirm: bool = False,
                                note: Annotated[str, Field(max_length=2000)] = '') -> dict[str, Any]:
        """管理员/本机显式修复 owner 或标记 needs_operator；需要 confirm=true。"""
        return await request('POST', '/api/admin/tasks/' + path_id(task_id, '任务') + '/repair-owner',
                             json={'confirm': confirm, 'note': note})

    @server.tool(annotations=write_record)
    async def resolve_ownerless_task(project_id: str, task_id: str, confirm_not_submitted: bool = False,
                                     note: Annotated[str, Field(max_length=2000)] = '') -> dict[str, Any]:
        """项目负责人确认冻结任务从未提交；需要 confirm_not_submitted=true。"""
        return await request('POST', project_path(project_id) + '/tasks/' + path_id(task_id, '任务') + '/resolve-ownerless',
                             json={'confirm_not_submitted': confirm_not_submitted, 'note': note})

    @server.tool(annotations=read_only)
    async def get_continuity_capability(project_id: str, segment_id: str) -> dict[str, Any]:
        """读取尾帧承接能力与候选事实；不抽取、不生成。"""
        return await request('GET', project_path(project_id) + '/segments/' + path_id(segment_id, '镜头') + '/continuity-capability')

    @server.tool(annotations=write_record)
    async def carry_frame(project_id: str, segment_id: str, source_segment_id: str,
                          source_slot: Literal['sample', 'finish', 'final']) -> dict[str, Any]:
        """把指定来源镜头已采用候选的尾帧登记为目标镜头首帧；不自动生成。"""
        return await request('POST', project_path(project_id) + '/segments/' + path_id(segment_id, '镜头') + '/carry-frame',
                             json={'source_segment_id': source_segment_id, 'source_slot': source_slot,
                                   'target_slot': 'first_frame'})

    @server.tool(annotations=read_only)
    async def list_audio_assets(project_id: str) -> dict[str, Any]:
        """列出本项目已登记音频素材；不归一、不替换装配。"""
        return await request('GET', project_path(project_id) + '/audio/assets')

    @server.tool(annotations=write_record)
    async def extract_audio(project_id: str, source_asset_id: str,
                            source_slot: Literal['sample', 'finish', 'final'],
                            audio_asset_id: Annotated[str | None, Field(max_length=160)] = None) -> dict[str, Any]:
        """从已采用视频候选抽取音轨登记为项目音频素材；不归一、不替换装配。"""
        body: dict[str, Any] = {'source_asset_id': source_asset_id, 'source_slot': source_slot}
        if audio_asset_id is not None:
            body['audio_asset_id'] = audio_asset_id
        return await request('POST', project_path(project_id) + '/audio/extract', json=body)
