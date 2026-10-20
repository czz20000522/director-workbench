"""只读项目恢复计划：逐镜头状态、候选/采用事实与装配阻断。

从 backend/app.py 拆出的领域路由沿用 private_mode 与既有 routers 的做法：只在
调用时通过 ``backend.<名称>`` 读取工作台级对象，因此 app.py 既有的 monkeypatch
语义不变，也不会在模块导入期形成循环依赖。本模块只注册读取路由；Lead 负责在
app.py 的既有位置 include_router。

本接口不重试任务、不采用候选、不写计划、不修复回执：所有写操作都留在返回的
显式动作所指向的既有端点里，由调用方自己确认后执行。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import app as backend
from .. import recovery


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/resume-plan')
def project_resume_plan(project_id: str) -> dict[str, Any]:
    """Return read-only resume facts for one authorized project."""
    return recovery.build_resume_plan(backend, project_id)
