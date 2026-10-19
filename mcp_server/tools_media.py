"""Audio editing and media inspection tools."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool


def register(server: MCPServer, client: Any, *, read_only: ToolAnnotations, write_record: ToolAnnotations) -> None:
    request = client.request
    project_path = client.project_path
    path_id = client.path_id

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

    @server.tool(annotations=write_record)
    async def submit_audio_master(project_id: str, source_asset_id: str, idempotency_key: str,
                                  background_asset_id: str | None = None, target_lufs: float = -16,
                                  true_peak_dbtp: float = -1, fade_in_seconds: float = 0,
                                  fade_out_seconds: float = 0, trim_start_seconds: float = 0,
                                  trim_duration_seconds: float | None = None, background_gain_db: float = -12,
                                  ducking: dict[str, float] | None = None, pad_silence: bool = False) -> dict[str, Any]:
        """对当前作品已登记的完整母版做最长30分钟CPU后期，产生待试听候选。
        使用确定性的响度/峰值归一化；背景必须是当前作品登记音频且格式相同。
         支持 WAV、MP3、M4A、FLAC；WAV 保留原采样率和声道，其他格式由 FFmpeg 归一化为 48kHz 双声道 PCM16。
         不创建自由多轨，也不会自动选择为装配完整母版。未知响应先核对原请求键。
        """
        if not source_asset_id.strip() or not idempotency_key.strip():
            raise ValueError('必须提供完整母版素材 ID 与请求标识')
        return await request('POST', project_path(project_id) + '/audio-master-tasks', json={
            'source_asset_id': source_asset_id, 'background_asset_id': background_asset_id,
            'idempotency_key': idempotency_key, 'target_lufs': target_lufs,
            'true_peak_dbtp': true_peak_dbtp, 'fade_in_seconds': fade_in_seconds,
            'fade_out_seconds': fade_out_seconds, 'trim_start_seconds': trim_start_seconds,
            'trim_duration_seconds': trim_duration_seconds, 'background_gain_db': background_gain_db,
            'ducking': ducking, **({'pad_silence': True} if pad_silence else {}),
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
