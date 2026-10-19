"""Reference import, analysis, and correction tools."""
from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

if __package__:
    from . import errors, media
else:
    import errors, media


MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_BASE64 = 22_369_624


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations,
             idempotent_write: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

    def finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f'{label}必须是有限数字')
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{label}必须是有限数字') from exc
        if not math.isfinite(result):
            raise ValueError(f'{label}必须是有限数字')
        return result

    def valid_analysis_id(analysis_id: str) -> None:
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')

    async def shape_analysis_result(result: dict[str, Any], project_id: str) -> dict[str, Any]:
        if not client.remote or not result.get('ok') or not isinstance(result.get('data'), dict):
            return result
        try:
            result['data'] = await media.shape_analysis(client, project_id, result['data'])
        except ValueError:
            result['data'] = media.redact_paths(result['data'])
        return result

    async def shape_observation_result(result: dict[str, Any], project_id: str, analysis_id: str) -> dict[str, Any]:
        if not client.remote or not result.get('ok') or not isinstance(result.get('data'), dict):
            return result
        data = result['data']
        observation = data.get('observation') if isinstance(data.get('observation'), dict) else None
        if observation is None:
            return result
        shaped = await media.shape_observation(client, project_id, analysis_id, observation)
        result['data'] = {'observation': shaped,
                          'analysis': media.analysis_identity(data.get('analysis') or {})}
        return result

    async def shape_job_result(result: dict[str, Any]) -> dict[str, Any]:
        if not client.remote or not result.get('ok') or not isinstance(result.get('data'), dict):
            return result
        if 'job' in result['data']:
            result['data'] = {**result['data'], 'job': media.public_job(result['data'].get('job'))}
        return result

    @server.tool(annotations=idempotent_write)
    async def import_reference_from_material(project_id: str, asset_id: str) -> dict[str, Any]:
        """把本作品已上传的音频/视频素材登记为参考拆解，远程 Agent 无需服务器路径。
        只做媒体探测和参考登记，不抽帧、不切段、不运行 ASR 或语义模型，也不改制作分镜。
        同一素材已有拆解时返回原拆解（created=false），可安全核对而不是重复登记。
        """
        if not asset_id.strip() or len(asset_id) > 160:
            raise ValueError('必须提供有效素材 ID')
        result = await request('POST', project_path(project_id) + '/reverse-analyses/from-material',
                               json={'asset_id': asset_id})
        if not result.get('ok') or not isinstance(result.get('data'), dict):
            return result
        data = result['data']
        analysis = data.get('analysis') if isinstance(data.get('analysis'), dict) else {}
        shaped = await shape_analysis_result({'ok': True, 'data': analysis}, project_id)
        return {'ok': True, 'data': {'analysis': shaped.get('data'), 'created': data.get('created')}}

    @server.tool(annotations=write_record)
    async def import_reference_content(project_id: str, filename: str, content_base64: str) -> dict[str, Any]:
        """上传客户端参考素材内容并登记参考拆解，无需服务器路径。
        单文件上限16MiB；超限返回明确错误，请改用HTTP multipart参考上传。仅登记媒体事实，
        不自动拆分、不抽帧、不运行ASR或转分镜。非幂等：回执未知先 list_references 核对。
        """
        if not filename or '/' in filename or chr(92) in filename or len(filename) > 160:
            raise ValueError('必须提供不含路径的参考素材文件名')
        if Path(filename).suffix.lower() not in {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.mp3', '.wav', '.flac', '.m4a', '.aac'}:
            raise ValueError('只支持常见音视频参考素材')
        if len(content_base64) > MAX_REFERENCE_BASE64:
            return errors.error('reference_too_large', 'MCP参考素材最多16MiB，请使用HTTP multipart参考上传',
                                kind='argument',
                                http_fallback='POST /api/projects/{project_id}/reverse-analyses 的 multipart file 字段')
        try:
            content = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise ValueError('参考素材内容必须是有效base64') from exc
        if not content or len(content) > MAX_REFERENCE_BYTES:
            return errors.error('reference_too_large', 'MCP参考素材须为非空、至多16MiB，请使用HTTP multipart上传较大文件',
                                kind='argument',
                                http_fallback='POST /api/projects/{project_id}/reverse-analyses 的 multipart file 字段')
        result = await request('POST', project_path(project_id) + '/reverse-analyses',
                               files={'file': (filename, content, 'application/octet-stream')})
        return await shape_analysis_result(result, project_id)

    @server.tool(annotations=write_record)
    async def import_reference(project_id: str, file_path: str) -> dict[str, Any]:
        """登记 MCP 服务所在机器上的音视频文件并保存原始媒体事实。提供绝对路径，不接受 URL；上传只做媒体探测和素材登记，不抽帧、不切段、不运行 ASR 或语义模型。不是幂等操作：丢失回执后先 list_references 核对，不能自动重新导入。"""
        endpoint = project_path(project_id) + '/reverse-analyses'
        path = Path(file_path)
        if not path.is_absolute() or path.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.mp3', '.wav', '.flac', '.m4a', '.aac'}:
            return errors.error('invalid_reference_file', '请提供 MCP 所在机器上音视频文件的绝对路径',
                                kind='argument')
        try:
            with path.open('rb') as source:
                return await request('POST', endpoint, files={'file': (path.name, source, 'application/octet-stream')}, timeout=960)
        except OSError:
            return errors.error('reference_file_unreadable', '参考文件无法读取；若上传已开始，请先查询参考列表核对',
                                kind='argument')

    @server.tool(annotations=write_record)
    async def extract_reference_range(project_id: str, analysis_id: str, start_seconds: float,
                                      end_seconds: float, frame_seconds: float | None = None) -> dict[str, Any]:
        """按调用方给定的起止时间提取参考片段，可选地在同一显式范围内提取一帧。结果是只读观察，不能自动成为制作分镜来源。远程返回观察身份及受控媒体链接，不返回服务器路径。"""
        valid_analysis_id(analysis_id)
        start = finite_number(start_seconds, '开始时间')
        end = finite_number(end_seconds, '结束时间')
        if start < 0 or end <= start:
            raise ValueError('必须提供满足 0 <= start < end 的显式时间段')
        body = {'start_seconds': start, 'end_seconds': end}
        if frame_seconds is not None:
            frame = finite_number(frame_seconds, '帧时间')
            if not start <= frame <= end:
                raise ValueError('帧时间必须位于显式参考时间段内')
            body['frame_seconds'] = frame
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/observations/range', json=body)
        return await shape_observation_result(result, project_id, analysis_id)

    @server.tool(annotations=write_record)
    async def extract_reference_audio(project_id: str, analysis_id: str,
                                      start_seconds: float, end_seconds: float) -> dict[str, Any]:
        """按调用方给定的起止时间提取音频观察，不运行 ASR，也不自动进入制作分镜。远程返回观察身份及受控媒体链接。"""
        valid_analysis_id(analysis_id)
        start = finite_number(start_seconds, '开始时间')
        end = finite_number(end_seconds, '结束时间')
        if start < 0 or end <= start:
            raise ValueError('必须提供满足 0 <= start < end 的显式音频范围')
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/observations/audio',
                               json={'start_seconds': start, 'end_seconds': end})
        return await shape_observation_result(result, project_id, analysis_id)

    @server.tool(annotations=write_record)
    async def sample_reference_frames(project_id: str, analysis_id: str, times_seconds: list[float]) -> dict[str, Any]:
        """按调用方明确给出的时间点采样参考帧。采样结果标记为“等距采样/待确认范围”或待校订观察，绝不当作镜头切点。远程返回观察身份及受控媒体链接。"""
        valid_analysis_id(analysis_id)
        if not times_seconds or len(times_seconds) > 64:
            raise ValueError('需要1到64个非负采样时间点')
        times = [finite_number(value, '采样时间') for value in times_seconds]
        if any(value < 0 for value in times):
            raise ValueError('需要1到64个非负采样时间点')
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/observations/sample', json={'times_seconds': times})
        return await shape_observation_result(result, project_id, analysis_id)

    @server.tool(annotations=write_record)
    async def transcribe_reference(project_id: str, analysis_id: str) -> dict[str, Any]:
        """显式运行参考音轨 ASR。返回模型候选/待校订观察，不能当作原词或普通事实；远程返回观察身份及受控媒体链接。"""
        valid_analysis_id(analysis_id)
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/observations/asr', json={})
        return await shape_observation_result(result, project_id, analysis_id)

    @server.tool(annotations=read_only, meta=errors.PAGINATION_META)
    async def list_references(project_id: str,
                              limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20,
                              offset: Annotated[int, Field(strict=True, ge=0)] = 0) -> dict[str, Any]:
        """分页列出作品已有参考拆解摘要；用 read_reference 读取时间线、分析工件或分析任务。不会上传或启动分析。"""
        result = await request('GET', project_path(project_id) + '/reverse-analyses')
        if not result['ok']:
            return result
        analyses = result['data']['analyses']
        rows = [{key: item.get(key) for key in ('id', 'title', 'status', 'created_at')}
                for item in analyses[offset:offset + limit]]
        for row, item in zip(rows, analyses[offset:offset + limit]):
            source = item.get('source') if isinstance(item.get('source'), dict) else {}
            row['source'] = media.redact_paths({key: source.get(key) for key in (
                'original_name', 'size_bytes', 'duration_seconds', 'width', 'height', 'fps',
                'has_video', 'has_audio', 'material_asset_id') if key in source})
        return {'ok': True, 'data': {'analyses': rows, 'total': len(analyses), 'next_offset': offset + limit if offset + limit < len(analyses) else None}}

    @server.tool(annotations=read_only)
    async def read_reference(project_id: str, analysis_id: str, section: Literal['analysis', 'semantic_job'] = 'analysis') -> dict[str, Any]:
        """读取参考拆解的事实、模型候选、人工校订及时间线，或查询内容分析回执。保留 provenance/user_status 与观察/工件身份；远程响应以受控媒体引用替代服务器路径，不把模型候选视为人工确认。"""
        valid_analysis_id(analysis_id)
        path = project_path(project_id) + '/reverse-analyses/' + analysis_id
        result = await request('GET', path + ('/semantic-jobs/current' if section == 'semantic_job' else ''))
        if section == 'semantic_job':
            return await shape_job_result(result)
        return await shape_analysis_result(result, project_id)

    @server.tool(annotations=write_record)
    async def start_reference_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        """启动已导入参考的后台内容分析，可能使用本机模型资源。返回任务回执而非分析完成结果；用 read_reference(section='semantic_job') 查询。已有活动任务返回原任务；完成后的再次调用会启动新分析，回执不确定时禁止盲目重发。"""
        valid_analysis_id(analysis_id)
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/semantic-jobs')
        return await shape_job_result(result)

    @server.tool(annotations=idempotent_write)
    async def reconcile_reference_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        """核对参考分析的独立完成回执并恢复原任务，不重新运行分析。仅用于 needs_reconcile；409 表示尚不能确认，保留待核对状态，不能据此重启分析。"""
        valid_analysis_id(analysis_id)
        result = await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/semantic-jobs/reconcile')
        return await shape_job_result(result)


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
    async def convert_reference_to_shots(project_id: str, analysis_id: str, expected_revision: int,
                                         shots: list[dict[str, Any]]) -> dict[str, Any]:
        """只把调用方明确提交的非空分镜列表转为制作草案。每项必须包含 start_seconds、end_seconds 和 fields；参考观察、等距采样和模型候选不会自动填入 performance/prompt。"""
        if not analysis_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in analysis_id):
            raise ValueError('必须提供有效参考拆解 ID')
        if expected_revision < 0 or not shots:
            raise ValueError('需要非负分析版本及至少一个明确制作分镜')
        return await request('POST', project_path(project_id) + '/reverse-analyses/' + analysis_id + '/convert', json={'expected_revision': expected_revision, 'shots': shots})
