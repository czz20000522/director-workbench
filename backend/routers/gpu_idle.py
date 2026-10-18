"""GPU 空闲释放：显存/队列空闲判定与 ComfyUI 显存回收请求。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from typing import Any
from fastapi import APIRouter
import logging
import math
import os
from .. import background_jobs, task_scheduler
from .. import app as backend


ROUTER = APIRouter()


def gpu_idle_reason() -> str | None:
    """Inspect every user's GPU work and ComfyUI's whole queue; unknown is an error."""
    if backend.REFERENCE_DECOMPOSE_ACTIVE or backend.REVERSE_SEMANTIC_LOCK.locked():
        return "参考分析仍在运行"
    release_statuses = tuple(status for status in backend.RELEASE_BLOCKING_STATUSES if status not in task_scheduler.WAITING)
    placeholders = ",".join("?" for _ in release_statuses)
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(f"SELECT payload FROM tasks WHERE status IN ({placeholders})", release_statuses).fetchall()
    if any(json.loads(row['payload']).get('kind') not in {'audio_edit', 'media_operation'} for row in rows):
        return "工作台 GPU 任务仍在执行或待核对"
    with background_jobs.LOCK:
        jobs = background_jobs.records(backend.analysis_job_root())
    if any(job['status'] in background_jobs.ACTIVE and not job.get('task_id') for job in jobs):
        return "参考语义分析任务仍在执行或待核对"
    if backend.TASK_SCHEDULER.runnable_waiting():
        return '已有可派发 GPU 任务等待资源'
    queue = backend.request_json('/queue')
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise ValueError('ComfyUI 队列回执不完整')
    if queue['queue_running'] or queue['queue_pending']:
        return "ComfyUI 队列仍有运行或待处理任务"
    return None


def gpu_free_mib() -> int | None:
    stats = backend.request_json('/system_stats')
    if not isinstance(stats, dict) or not isinstance(stats.get('devices'), list) or not stats['devices']:
        return None
    free_bytes = stats['devices'][0].get('vram_free')
    return round(float(free_bytes) / 2**20) if free_bytes is not None else None


def comfy_history_token() -> str:
    """Notice a short direct-ComfyUI job even when both queue polls miss it."""
    history = backend.request_json('/history?max_items=1')
    if not isinstance(history, dict) or len(history) > 1 or any(not isinstance(item, dict) for item in history.values()):
        raise ValueError('ComfyUI 最近任务历史回执不完整')
    return next(iter(history), '')


def request_comfy_free() -> dict[str, Any]:
    return backend.request_json('/free', {'unload_models': True, 'free_memory': True})


def gpu_idle_setting(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError('must be positive')
        return value
    except ValueError:
        logging.warning('Invalid %s; using %.0f seconds', name, default)
        return default
