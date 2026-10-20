"""显式分镜资源包绑定与资源包首帧使用的 HTTP 入口。

领域合同在 ``backend/resource_bindings.py``：资源包必须属于作品系列、已发布且
当前用户可见；绑定只写 ``plan.segment.resources``，首帧使用才把已发布包图片
复制进项目并按现有规范登记 image asset。路由不提交生成、不采用候选。由 app.py
在既有路由区接入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import resource_bindings
from ..resource_bindings import ResourceBindingRequest, UseResourceFrameRequest


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/segments/{segment_id}/resources')
def read_segment_resources(project_id: str, segment_id: str) -> dict[str, Any]:
    """回显该分镜已显式绑定的资源包事实；只读，不生成。"""
    try:
        return resource_bindings.read_resources(project_id, segment_id)
    except resource_bindings.ResourceBindingError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@ROUTER.put('/api/projects/{project_id}/segments/{segment_id}/resources')
@backend.plan_transaction
def bind_segment_resources(project_id: str, segment_id: str,
                           request: ResourceBindingRequest) -> dict[str, Any]:
    """显式写入 character/scene/sound 资源包绑定；不自动生成或自动使用资源。"""
    try:
        return resource_bindings.bind_resources(project_id, segment_id, request)
    except resource_bindings.ResourceBindingError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@ROUTER.post('/api/projects/{project_id}/segments/{segment_id}/use-resource-frame')
@backend.plan_transaction
def use_segment_resource_frame(project_id: str, segment_id: str,
                               request: UseResourceFrameRequest) -> dict[str, Any]:
    """把已发布资源包中的图片复制并登记为该分镜首帧；不提交生成。"""
    try:
        return resource_bindings.use_resource_frame(project_id, segment_id, request)
    except resource_bindings.ResourceBindingError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
