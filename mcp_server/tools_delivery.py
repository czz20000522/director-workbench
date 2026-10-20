"""Recovery, audio delivery, upscale and shot-refinement tools."""
from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations,
             idempotent_write: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

    @server.tool(annotations=read_only)
    async def plan_resume(project_id: str) -> dict[str, Any]:
        """读取项目恢复计划：逐镜头状态、阻断、候选与显式下一步；不自动重试、不自动采用、不自动装配。"""
        return await request('GET', project_path(project_id) + '/resume-plan')

    @server.tool(annotations=read_only)
    async def analyze_audio(project_id: str,
                            asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                            source_asset_id: Annotated[str, Field(min_length=1, max_length=160)]) -> dict[str, Any]:
        """测量显式音频源的 integrated LUFS、true peak 与 LRA；只读，不生成候选、不修改装配。"""
        return await request('GET', project_path(project_id) + '/audio/analysis',
                             params={'asset_id': asset_id, 'source_asset_id': source_asset_id})

    @server.tool(annotations=write_record)
    async def normalize_audio(project_id: str,
                              asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                              source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                              target_lufs: Annotated[float, Field(ge=-24, le=-8)],
                              true_peak_db: Annotated[float, Field(ge=-3, le=0)],
                              idempotency_key: Annotated[str | None, Field(max_length=128)] = None) -> dict[str, Any]:
        """按显式 target_lufs/true_peak_db 生成新的音频候选；不覆盖原音、不自动替换装配。"""
        body: dict[str, Any] = {'asset_id': asset_id, 'source_asset_id': source_asset_id,
                                'target_lufs': target_lufs, 'true_peak_db': true_peak_db}
        if idempotency_key is not None:
            body['idempotency_key'] = idempotency_key
        return await request('POST', project_path(project_id) + '/audio/normalize', json=body)

    @server.tool(annotations=read_only)
    async def get_upscale_capability(project_id: str) -> dict[str, Any]:
        """读取 2K/4K 交付超分能力、方法与阻断；不选择目标、不提交任务。"""
        return await request('GET', project_path(project_id) + '/upscale-capability')

    @server.tool(annotations=write_record)
    async def submit_upscale(project_id: str,
                             source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                             source_slot: Annotated[str, Field(min_length=1, max_length=16)],
                             target_resolution: Annotated[str, Field(min_length=1, max_length=8)],
                             method: Annotated[str, Field(min_length=1, max_length=32)],
                             preserve_audio: bool,
                             idempotency_key: Annotated[str, Field(min_length=1, max_length=160)]) -> dict[str, Any]:
        """按显式源、目标、方法生成新的 2K/4K 候选；不覆盖源、不自动采用/装配。"""
        return await request('POST', project_path(project_id) + '/upscale-tasks', json={
            'source_asset_id': source_asset_id, 'source_slot': source_slot,
            'target_resolution': target_resolution, 'method': method,
            'preserve_audio': preserve_audio, 'idempotency_key': idempotency_key})

    @server.tool(annotations=read_only)
    async def get_refinement_capability(project_id: str, segment_id: str) -> dict[str, Any]:
        """读取镜头局部重做方法、所需输入与阻断；不选择方法、不提交生成。"""
        return await request('GET', project_path(project_id) + '/segments/' + path_id(segment_id, '镜头') + '/refinement-capability')

    @server.tool(annotations=write_record)
    async def submit_refinement(project_id: str, segment_id: str,
                                method: Annotated[str, Field(min_length=1, max_length=64)],
                                base_candidate_ref: Annotated[str, Field(min_length=1, max_length=1000)],
                                instruction: Annotated[str, Field(min_length=1, max_length=20000)],
                                interval: dict[str, Any] | None = None,
                                preserve: list[str] | None = None) -> dict[str, Any]:
        """登记显式镜头重做请求；第一版只登记、不提交生成、不自动采用；方法不可用返回结构化 blocker。"""
        body: dict[str, Any] = {'method': method, 'base_candidate_ref': base_candidate_ref,
                                'instruction': instruction, 'preserve': preserve or []}
        if interval is not None:
            body['interval'] = interval
        return await request('POST', project_path(project_id) + '/segments/' + path_id(segment_id, '镜头') + '/refinements', json=body)
