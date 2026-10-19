"""Audio editing, media inspection and controlled media delivery tools."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool

if __package__:
    from . import errors, media
else:
    import errors, media


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations,
             idempotent_write: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

    async def shape_task_result(result: dict[str, Any], project_id: str) -> dict[str, Any]:
        if not client.remote or not result.get('ok') or not isinstance(result.get('data'), dict):
            return result
        result['data'] = await media.shape_task(client, project_id, result['data'])
        return result

    @server.tool(annotations=read_only, meta=errors.PAGINATION_META)
    async def list_media(project_id: str,
                         category: Literal['all', 'assets', 'segments', 'assembly', 'references', 'tasks'] = 'all',
                         kind: Literal['all', 'image', 'audio', 'video', 'file'] = 'all',
                         limit: Annotated[int, Field(ge=1, le=100)] = 50,
                         offset: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """分页列出本作品可读取的候选视频/音频/图片、成片、审片和参考观察媒体。
        只返回稳定 media_ref、文件事实和短期受控 URL，不返回服务器路径；媒体读取仍需本人会话。
        用 next_offset 继续；任务产物通过 category='tasks' 读取。不会采用或审核任何候选。
        """
        return await request('GET', project_path(project_id) + '/media',
                             params={'category': category, 'kind': kind, 'limit': limit, 'offset': offset})

    @server.tool(annotations=read_only)
    async def get_media_url(project_id: str,
                            media_ref: Annotated[str, Field(min_length=1, max_length=512, pattern=r'\S')]) -> dict[str, Any]:
        """按稳定 media_ref 重新签发短期受控 URL（默认15分钟），用于候选查看和交付。
        media_ref 由 list_media、get_task、read_reference 或观察工具返回；不接受服务器路径。
        返回文件类型、大小和 URL；URL 仍需同一 Bearer 会话，过期后重新调用即可。
        """
        refs = await media.issue_media(client, project_id, [media_ref])
        entry = refs.get(media_ref)
        if entry is None:
            return errors.error('media_ref_unavailable', '媒体引用不存在、无权访问或文件已变化',
                                kind='business')
        return {'ok': True, 'data': entry}

    @server.tool(annotations=read_only)
    async def download_media(project_id: str,
                             media_ref: Annotated[str, Field(min_length=1, max_length=512, pattern=r'\S')],
                             max_inline_bytes: Annotated[int, Field(ge=1, le=media.MAX_INLINE_MEDIA_LIMIT)] = media.INLINE_MEDIA_LIMIT) -> dict[str, Any]:
        """读取一个受控媒体文件：小文件直接返回 base64 内容，大文件只返回短期受控 URL。
        不会回传服务器路径，也不会为超大文件拼接 base64。需要原文件时按返回 url 下载；
        url 过期后重新调用 get_media_url。媒体内容本身不代表已审核或已采用。
        """
        try:
            value = await media.inline_media(client, project_id, media_ref, max_inline_bytes)
        except ValueError as exc:
            return errors.error('invalid_inline_budget', str(exc), kind='argument')
        if value.get('ok') is False:
            return value
        return {'ok': True, 'data': value}

    @server.tool(annotations=idempotent_write)
    async def submit_audio_edit(project_id: str,
                                source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                                idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')],
                                background_asset_id: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
                                start_seconds: Annotated[float, Field(ge=0, le=86400)] = 0,
                                duration_seconds: Annotated[float | None, Field(gt=0, le=120)] = None,
                                gain: Annotated[float, Field(ge=0, le=8)] = 1,
                                fade_in_seconds: Annotated[float, Field(ge=0, le=120)] = 0,
                                fade_out_seconds: Annotated[float, Field(ge=0, le=120)] = 0,
                                background_gain: Annotated[float, Field(ge=0, le=8)] = .25,
                                background_offset_seconds: Annotated[float, Field(ge=0, le=120)] = 0,
                                pad_silence: bool = False) -> dict[str, Any]:
        """编辑本作品登记的音频素材，产生独立待试听候选，不覆盖或自动绑定原音轨。
        CPU处理PCM16 WAV或最多120秒MP3；MP3转48kHz双声道，背景须同采样率及声道。
        增益为线性倍数；源不足时仅pad_silence=true才补静音。完整母版替换各段原声，不自动保留对白。
        原请求键和参数不可变；未知响应先get_submission_receipt核对，不换键重复编辑。
        """
        result = await request('POST', project_path(project_id) + '/audio-edit-tasks', json={
            'source_asset_id': source_asset_id, 'background_asset_id': background_asset_id,
            'idempotency_key': idempotency_key, 'start_seconds': start_seconds,
            'duration_seconds': duration_seconds, 'gain': gain,
            'fade_in_seconds': fade_in_seconds, 'fade_out_seconds': fade_out_seconds,
            'background_gain': background_gain, 'background_offset_seconds': background_offset_seconds,
            **({'pad_silence': True} if pad_silence else {}),
        })
        return await shape_task_result(result, project_id)

    @server.tool(annotations=idempotent_write)
    async def submit_audio_master(project_id: str,
                                  source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                                  idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')],
                                  background_asset_id: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
                                  target_lufs: Annotated[float, Field(ge=-24, le=-8)] = -16,
                                  true_peak_dbtp: Annotated[float, Field(ge=-3, le=0)] = -1,
                                  fade_in_seconds: Annotated[float, Field(ge=0, le=10)] = 0,
                                  fade_out_seconds: Annotated[float, Field(ge=0, le=10)] = 0,
                                  trim_start_seconds: Annotated[float, Field(ge=0, le=3600)] = 0,
                                  trim_duration_seconds: Annotated[float | None, Field(gt=0, le=1800)] = None,
                                  background_gain_db: Annotated[float, Field(ge=-60, le=12)] = -12,
                                  ducking: dict[str, float] | None = None, pad_silence: bool = False) -> dict[str, Any]:
        """对当前作品已登记的完整母版做最长30分钟CPU后期，产生待试听候选。
        使用确定性的响度/峰值归一化；背景必须是当前作品登记音频且格式相同。
         支持 WAV、MP3、M4A、FLAC；WAV 保留原采样率和声道，其他格式由 FFmpeg 归一化为 48kHz 双声道 PCM16。
         不创建自由多轨，也不会自动选择为装配完整母版。未知响应先核对原请求键。
        """
        result = await request('POST', project_path(project_id) + '/audio-master-tasks', json={
            'source_asset_id': source_asset_id, 'background_asset_id': background_asset_id,
            'idempotency_key': idempotency_key, 'target_lufs': target_lufs,
            'true_peak_dbtp': true_peak_dbtp, 'fade_in_seconds': fade_in_seconds,
            'fade_out_seconds': fade_out_seconds, 'trim_start_seconds': trim_start_seconds,
            'trim_duration_seconds': trim_duration_seconds, 'background_gain_db': background_gain_db,
            'ducking': ducking, **({'pad_silence': True} if pad_silence else {}),
        })
        return await shape_task_result(result, project_id)

    @server.tool(annotations=read_only)
    async def get_media_capability() -> dict[str, Any]:
        """读取静音、视频抽帧/技术检查、台词转写及无声导出能力与缺失资源。"""
        return await request('GET', '/api/media-capability')

    async def media_submit(project_id: str, idempotency_key: str, operation: str, **parameters: Any) -> dict[str, Any]:
        result = await request('POST', project_path(project_id) + '/media-tasks', json={
            'operation': operation, 'idempotency_key': idempotency_key, **parameters,
        })
        return await shape_task_result(result, project_id)

    @server.tool(annotations=idempotent_write)
    async def create_silence(project_id: str,
                             idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')],
                             duration_seconds: Annotated[float, Field(gt=0, le=120)],
                             sample_rate: Literal[16000, 24000, 48000] = 24000,
                             channels: Literal[1, 2] = 1) -> dict[str, Any]:
        """生成本作静音WAV候选。先保存请求键；未知响应使用get_submission_receipt，不换键重发。"""
        return await media_submit(project_id, idempotency_key, 'silence', duration_seconds=duration_seconds,
                                  sample_rate=sample_rate, channels=channels)

    @server.tool(annotations=idempotent_write)
    async def inspect_video(project_id: str,
                            source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                            idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')],
                            sample_count: Annotated[int, Field(ge=3, le=60)] = 12) -> dict[str, Any]:
        """对本作视频素材ID执行技术检查和抽帧；报告不是画面/表演人工验收。未知响应核对原请求键。"""
        return await media_submit(project_id, idempotency_key, 'video_qc', source_asset_id=source_asset_id, sample_count=sample_count)

    @server.tool(annotations=idempotent_write)
    async def transcribe_audio(project_id: str,
                               source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                               idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')],
                               expected_text: Annotated[str, Field(max_length=2000)] = '',
                               language: Literal['zh', 'en', 'auto'] = 'zh',
                               word_timestamps: StrictBool = True) -> dict[str, Any]:
        """转写本作音频或视频，返回时间戳和预期台词差异提示；不能代替试听或批准音色。未知响应核对原请求键。"""
        return await media_submit(project_id, idempotency_key, 'transcribe', source_asset_id=source_asset_id,
                                  expected_text=expected_text, language=language, word_timestamps=word_timestamps)

    @server.tool(annotations=idempotent_write)
    async def export_silent_video(project_id: str,
                                  source_asset_id: Annotated[str, Field(min_length=1, max_length=160)],
                                  idempotency_key: Annotated[str, Field(min_length=1, max_length=160, pattern=r'\S')]) -> dict[str, Any]:
        """导出本作视频的无声审片候选，不覆盖原片。未知响应先get_submission_receipt；后续get_task查看outputs/report。"""
        return await media_submit(project_id, idempotency_key, 'silent_video', source_asset_id=source_asset_id)
