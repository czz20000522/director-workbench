"""显式分镜局部动作重做的 HTTP 入口（由 app.py 在既有位置接入）。

领域合同在 ``backend/refinement.py``：能力查询只返回事实与阻断项，请求校验不
静默降级；本模块读取当前作品/计划并把通过校验的请求登记为 project artifact。
第一版不提交生成、不修改计划、不自动采用候选。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import refinement
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/segments/{segment_id}/refinement-capability')
def refinement_capability(project_id: str, segment_id: str) -> dict[str, Any]:
    """读取该镜头可用的局部重做方法、所需输入与结构化阻断项。

    只读：不提交生成、不修改计划、不替调用方选择方法。`interval_edit` 在第一版
    始终不可用，并返回结构化 blocker；调用方不能据此改用其他方法静默替代。
    """
    backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    segment = refinement.find_segment(document, segment_id)
    if segment is None:
        raise HTTPException(404, {'code': 'segment_not_found', 'message': '该作品中不存在此分镜。',
                                  'retryable': False})
    return refinement.capability(project_id, segment)


@ROUTER.post('/api/projects/{project_id}/segments/{segment_id}/refinements')
def create_refinement(project_id: str, segment_id: str,
                      request: refinement.RefinementRequest) -> dict[str, Any]:
    """校验并登记一次显式局部重做请求；第一版返回 accepted，但绝不提交生成。

    候选必须属于该镜头当前登记候选或生成历史且文件存在；区间只对
    `interval_edit` 有效；不可用方法返回结构化 blocker，不静默降级。
    """
    backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    segment = refinement.find_segment(document, segment_id)
    if segment is None:
        raise HTTPException(404, {'code': 'segment_not_found', 'message': '该作品中不存在此分镜。',
                                  'retryable': False})

    def resolve_candidate(value: str) -> Path:
        try:
            return backend.adoption_candidate_path(str(value), project_id)
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc
        except (OSError, RuntimeError) as exc:
            raise ValueError(str(exc)) from exc

    try:
        facts = refinement.validate_request(segment, request, resolve_candidate=resolve_candidate)
    except refinement.RefinementError as exc:
        raise HTTPException(exc.status_code, exc.payload()) from exc

    record = backend.append_project_record(
        'artifact:refinement',
        refinement.refinement_record(project_id, segment_id, facts),
        asset_id=segment_id,
        source='director-refinement',
        project_id=project_id,
    )
    revision = int(record.get('revision') or 0)
    return {
        'accepted': True,
        'effect': 'request_recorded',
        'generation_submitted': False,
        'refinement_id': f'{segment_id}:refinement:{revision}',
        'project_id': project_id,
        'segment_id': segment_id,
        'method': facts['method'],
        'base_candidate_ref': facts['base_candidate_ref'],
        'interval': facts['interval'],
        'preserve': facts['preserve'],
        'record': {
            'kind': 'refinement',
            'asset_id': segment_id,
            'revision': revision,
            'source': 'director-refinement',
            'created_at': record.get('created_at_iso'),
        },
        'note': '已登记显式重做请求；未提交生成，未修改计划，未自动采用候选。',
    }
