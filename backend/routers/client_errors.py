"""前端异常收集。

浏览器里的错误边界只有用户能看到控制台；这里提供一个写入点，把渲染异常、
脚本异常和 chunk 加载失败落到 `<数据根>/logs/client-errors.log`。
只做记录，不做告警、不影响任何业务状态。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import app as backend

ROUTER = APIRouter()

MAX_LOG_BYTES = 2_000_000


class ClientErrorRequest(BaseModel):
    # ignore：以后前端加字段（比如路由、版本号）不该被 422 挡住，日志宁可多不可丢。
    model_config = ConfigDict(extra='ignore')

    message: str = Field(default='', max_length=2000)
    stack: str = Field(default='', max_length=8000)
    component_stack: str = Field(default='', max_length=8000)
    url: str = Field(default='', max_length=1000)
    user_agent: str = Field(default='', max_length=500)
    kind: str = Field(default='render', max_length=40)


def log_path() -> Path:
    return Path(backend.ROOT) / 'logs' / 'client-errors.log'


@ROUTER.post('/api/client-errors')
def record_client_error(request: ClientErrorRequest) -> dict[str, bool]:
    folder = log_path().parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = log_path()
        # 单文件滚动：超过 2MB 就把旧文件挪到 .1（只留一份，够排查一次事故）。
        if target.is_file() and target.stat().st_size > MAX_LOG_BYTES:
            target.replace(target.with_suffix('.log.1'))
        entry = {
            'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            **request.model_dump(),
        }
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        return {'stored': False}
    return {'stored': True}


@ROUTER.get('/api/client-errors/recent')
def recent_client_errors(limit: int = 20) -> dict[str, list[dict]]:
    """给排查用：直接读最近几条，不用去翻文件。"""
    target = log_path()
    if not target.is_file():
        return {'entries': []}
    try:
        lines = target.read_text(encoding='utf-8', errors='replace').strip().splitlines()
    except OSError:
        return {'entries': []}
    entries: list[dict] = []
    for line in lines[-max(1, min(limit, 200)):]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return {'entries': entries}
