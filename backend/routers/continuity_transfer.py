"""显式跨镜头尾帧承接的 HTTP 入口。

领域合同在 ``backend/continuity_transfer.py``：只接受调用方明确给出的来源
分镜/槽位和目标首帧槽位，只使用已明确采用且仍绑定当前文件的来源候选，用
受控 FFmpeg 抽取尾帧并登记为目标首帧素材。路由本身不提交生成、不修改采用、
不装配。由 app.py 在既有路由区接入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import continuity_transfer
from ..continuity_transfer import CarryFrameRequest


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/segments/{segment_id}/continuity-capability')
def continuity_capability(project_id: str, segment_id: str) -> dict[str, Any]:
    """返回是否可承接，以及已明确采用的来源候选和帧位置事实。

    只读：不抽取帧、不修改计划、不替调用方选择来源/槽位。FFmpeg 不可用或
    没有可用来源时返回结构化 blocker。
    """
    try:
        return continuity_transfer.capability(project_id, segment_id)
    except continuity_transfer.ContinuityError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@ROUTER.post('/api/projects/{project_id}/segments/{segment_id}/carry-frame')
@backend.plan_transaction
def carry_frame(project_id: str, segment_id: str, request: CarryFrameRequest) -> dict[str, Any]:
    """把来源已采用候选的尾帧登记为目标分镜首帧。

    来源必须已明确采用且文件未变化；目标首帧写入计划并登记为图片素材。
    重复请求在来源身份和目标首帧均未变化时直接返回既有结果，不重复抽帧或
    增加计划 revision。未提交生成，未修改采用。
    """
    try:
        return continuity_transfer.carry_frame(project_id, segment_id, request)
    except continuity_transfer.ContinuityError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
