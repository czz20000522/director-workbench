"""Reference import, analysis, and correction tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

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
