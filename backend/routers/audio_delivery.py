"""显式音频响度分析与归一候选路由。

只创建候选并登记为项目音频素材；不修改分镜、不替换母版、不自动采用。
所有权校验沿用 ``backend.require_project_manifest`` 与 ``backend.audio_edit_source``，
因此私人模式下仍受现有中间件与路径归属规则约束。

本模块刻意只包含两个路由函数（模块拆分测试要求每个模块级定义都能从
backend.app 按同一对象再导出）。app.py 接入时在路由注册区加入：
    from .routers.audio_delivery import ROUTER as _audio_delivery_router
    app.include_router(_audio_delivery_router)
    from .routers.audio_delivery import get_project_audio_analysis, normalize_project_audio
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import audio_normalization
from ..audio_normalization import AudioNormalizeRequest


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/audio/analysis')
def get_project_audio_analysis(project_id: str, asset_id: str, source_asset_id: str) -> dict:
    """Measure integrated LUFS, true peak and LRA of one explicit source asset.

    ``asset_id`` only declares the intended binding (a segment id or MASTER);
    the analysis is read-only and never stores a candidate.
    """
    manifest = backend.require_project_manifest(project_id)
    plan, _ = backend.load_project_plan(project_id)
    try:
        binding = audio_normalization.validate_binding(asset_id, manifest, plan)
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_audio_binding', 'message': str(exc)}) from exc
    try:
        source, source_record = backend.audio_edit_source(manifest, source_asset_id)
    except HTTPException as exc:
        message = exc.detail if isinstance(exc.detail, str) else '音频源素材无效'
        raise HTTPException(exc.status_code, {'code': 'invalid_audio_source', 'message': message}) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, {'code': 'invalid_audio_source', 'message': str(exc)}) from exc
    status = audio_normalization.tool_status()
    if not status['available']:
        raise HTTPException(503, {'code': 'audio_tool_unavailable',
                                  'message': '本地 FFmpeg 不可用，无法分析或归一音频',
                                  'missing': status['missing'], 'method': status['method']})
    try:
        result = audio_normalization.analyze(source)
    except audio_normalization.AudioToolUnavailable as exc:
        raise HTTPException(503, {'code': 'audio_tool_unavailable', 'message': str(exc),
                                  'missing': exc.missing, 'method': audio_normalization.METHOD}) from exc
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_audio_source', 'message': str(exc)}) from exc
    except audio_normalization.AudioAnalysisFailed as exc:
        raise HTTPException(502, {'code': 'audio_analysis_failed', 'message': str(exc)}) from exc
    return {
        'project_id': project_id,
        'asset_id': binding['asset_id'],
        'binding': binding,
        'source_asset_id': source_asset_id,
        'source_asset': source_record,
        'source': result['source'],
        'available': True,
        'method': result['method'],
        'measurement': result['measurement'],
        'measured_at': result['measured_at'],
        'note': '响度分析只读；不会修改分镜、装配或候选采用状态。',
    }


@ROUTER.post('/api/projects/{project_id}/audio/normalize')
def normalize_project_audio(project_id: str, request: AudioNormalizeRequest) -> dict:
    """Create a new normalized audio candidate bound to a segment or MASTER."""
    manifest = backend.require_project_manifest(project_id)
    plan, _ = backend.load_project_plan(project_id)
    try:
        binding = audio_normalization.validate_binding(request.asset_id, manifest, plan)
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_audio_binding', 'message': str(exc)}) from exc
    try:
        target = audio_normalization.validate_target_parameters(request.target_lufs, request.true_peak_db)
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_audio_parameter', 'message': str(exc)}) from exc
    try:
        source, source_record = backend.audio_edit_source(manifest, request.source_asset_id)
    except HTTPException as exc:
        message = exc.detail if isinstance(exc.detail, str) else '音频源素材无效'
        raise HTTPException(exc.status_code, {'code': 'invalid_audio_source', 'message': message}) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, {'code': 'invalid_audio_source', 'message': str(exc)}) from exc
    status = audio_normalization.tool_status()
    if not status['available']:
        raise HTTPException(503, {'code': 'audio_tool_unavailable',
                                  'message': '本地 FFmpeg 不可用，无法分析或归一音频',
                                  'missing': status['missing'], 'method': status['method']})
    idempotency_key = (request.idempotency_key or '').strip() or None
    if idempotency_key:
        existing = audio_normalization.find_existing_candidate(
            manifest, idempotency_key=idempotency_key, binding=binding,
            source_asset_id=request.source_asset_id, target=target)
        if existing:
            try:
                registered = backend.resolve_registered_path(str((existing.get('sources') or {}).get('A')))
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(409, {'code': 'audio_candidate_missing',
                                          'message': f'既有候选文件不可读: {exc}'}) from exc
            if not registered.is_file():
                raise HTTPException(409, {'code': 'audio_candidate_missing',
                                          'message': '既有候选文件不存在，请重新生成本次归一'})
            return audio_normalization.candidate_response(existing, reused=True)
    workspace = backend.resolve_registered_path(str(manifest['workspace_root']))
    candidate_id = f"audio-normalization-{uuid.uuid4().hex[:12]}"
    output = workspace / 'audio-normalization' / f'{candidate_id}.wav'
    try:
        report = audio_normalization.normalize(
            source, output, target_lufs=target['target_lufs'], true_peak_db=target['true_peak_db'],
            output_root=workspace)
    except audio_normalization.AudioToolUnavailable as exc:
        raise HTTPException(503, {'code': 'audio_tool_unavailable', 'message': str(exc),
                                  'missing': exc.missing, 'method': audio_normalization.METHOD}) from exc
    except FileExistsError as exc:
        raise HTTPException(409, {'code': 'audio_candidate_exists', 'message': str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_audio_source', 'message': str(exc)}) from exc
    except (audio_normalization.AudioAnalysisFailed, audio_normalization.AudioNormalizationFailed) as exc:
        raise HTTPException(502, {'code': 'audio_normalization_failed', 'message': str(exc)}) from exc
    candidate = {
        'id': candidate_id,
        'kind': 'audio',
        'title': f"响度归一 · {binding['asset_id']}",
        'ready': True,
        'subtitle': f"响度归一候选 · {target['target_lufs']:g} LUFS / {target['true_peak_db']:g} dBTP · 待试听",
        'origin': 'CPU响度归一',
        'sources': {'A': backend.root_relative_path(output)},
        'start': 0,
        'end': report['duration_seconds'],
        'audio_normalization': {
            'method': report['method'],
            'project_id': project_id,
            'bound_asset_id': binding['asset_id'],
            'binding_kind': binding['kind'],
            'source_asset_id': request.source_asset_id,
            'source_asset': source_record,
            'source': report['source'],
            'target_lufs': target['target_lufs'],
            'true_peak_db': target['true_peak_db'],
            'idempotency_key': idempotency_key,
            'created_at': report['created_at'],
            'report': report,
            'input_measurement': report['input_measurement'],
            'output_measurement': report['output_measurement'],
            'true_peak_limit_met': report['true_peak_limit_met'],
            'normalization_warning': report['normalization_warning'],
        },
    }
    try:
        with backend.MATERIAL_REUSE_LOCK:
            current = backend.require_project_manifest(project_id)
            assets = list(current.get('assets') or [])
            assets.append(candidate)
            current['assets'] = assets
            backend.save_project_manifest(current)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(current)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(502, {'code': 'audio_candidate_registration_failed',
                                  'message': f'候选已生成但登记失败: {exc}'}) from exc
    return audio_normalization.candidate_response(candidate, reused=False)
