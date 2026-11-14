"""绘图引擎（ComfyUI）按需预热。

工作台不常驻 ComfyUI：冷启动要一分钟左右，第一个任务就得干等。这里给界面一个
「先帮我拉起来」的入口——打开生成面板时调一次，用户写提示词的时间正好覆盖启动。
只启动进程，不排队、不提交任何任务，也不需要显卡空着。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter

from .. import comfy_engine

ROUTER = APIRouter()

# 预热线程是否在跑。ensure_online 自带 LOCK：真正提交任务时如果预热还没完，
# 提交会等在同一把锁上，不会出现两个引擎进程。
_STARTING = threading.Event()
_LOCK = threading.Lock()
_STARTED_AT = 0.0
_ERROR = ''


def _warm() -> None:
    global _STARTED_AT, _ERROR
    _STARTED_AT = time.time()
    _ERROR = ''
    try:
        comfy_engine.ensure_online()
    except Exception as exc:  # noqa: BLE001 - 预热失败不该把线程打崩
        _ERROR = str(exc)
    finally:
        _STARTING.clear()


@ROUTER.post('/api/engine/warm')
def warm_engine() -> dict[str, Any]:
    """立刻返回，后台拉起引擎。重复调用是幂等的。"""
    if comfy_engine.online(timeout=0.6):
        return {'online': True, 'starting': False, 'error': ''}
    with _LOCK:
        if not _STARTING.is_set():
            _STARTING.set()
            threading.Thread(target=_warm, name='director-engine-warm', daemon=True).start()
    return {'online': False, 'starting': True, 'error': _ERROR}


@ROUTER.get('/api/engine/status')
def engine_status() -> dict[str, Any]:
    starting = _STARTING.is_set()
    online = comfy_engine.online(timeout=0.8)
    return {
        'online': online,
        'starting': starting,
        'waited_ms': int((time.time() - _STARTED_AT) * 1000) if starting and _STARTED_AT else 0,
        'error': '' if online else _ERROR,
    }
