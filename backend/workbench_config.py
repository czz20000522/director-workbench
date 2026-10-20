"""导演工作台模块级常量与共享锁。

这些名字原先定义在 backend/app.py 顶部。集中到一个只做声明、不导入 app 的模块，
既让 app.py 变小，也让别的模块读常量时不必进入 app 的导入环。app.py 仍然
`from .workbench_config import ...` 重新导出全部名字，所以既有引用、以及测试里的
monkeypatch.setattr(backend, "<常量>", ...) 行为完全不变。
不得在这里 import app：本模块是 app 的下层。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import paths

from .storage import storage_roots



# 路径根：工作台数据根、ComfyUI 机器根、项目根、运行目录与两个作品资产根。
ROOT = paths.DATA_ROOT
COMFY_ROOT = paths.COMFY_ROOT
SHARED_ROOT = paths.SHARED_ROOT
PROJECT = paths.PROJECT_ROOT
RUNTIME = paths.RUNTIME_ROOT / "director-workbench"
DB_PATH = RUNTIME / "director-workbench.sqlite3"
COMFY_URL = os.environ.get("DIRECTOR_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
TASK_EXECUTION_TIMEOUT_SECONDS = float(os.environ.get("DIRECTOR_TASK_EXECUTION_TIMEOUT_SECONDS", "7200"))
TASK_MONITOR_POLL_SECONDS = float(os.environ.get("DIRECTOR_TASK_MONITOR_POLL_SECONDS", "2"))
RENDERER = paths.SKILLS_ROOT / "skills" / "aigc-video-production" / "scripts" / "h3_render.py"
TEMPLATE = paths.SKILLS_ROOT / "skills" / "aigc-video-production" / "assets" / "workflows" / "minimax-h3-current-api.json"
STORAGE_ROOTS = storage_roots(ROOT, shared_root=SHARED_ROOT)
INPUT_ROOT = STORAGE_ROOTS['input']
OUTPUT_ROOT = STORAGE_ROOTS['output']
FINISH_BUILDER = PROJECT / "tools" / "build_video_finish_workflow.py"
DIST = PROJECT / "dist"
REFERENCE_DECOMPOSER = PROJECT / "tools" / "reference_decompose.py"
REFERENCE_SEMANTIC_ANALYZER = PROJECT / "tools" / "reference_semantic.py"

# 共享锁：与 app 生命周期无关，供路由与后台任务共同持有。
REVERSE_DOCUMENT_LOCK = threading.RLock()
REVERSE_SEMANTIC_LOCK = threading.Lock()
GPU_ADMISSION_LOCK = threading.RLock()
MATERIAL_REUSE_LOCK = threading.RLock()
PLAN_EDIT_LOCK = threading.RLock()
AUDIO_EDIT_LOCK = threading.RLock()
MEDIA_OPERATION_LOCK = threading.RLock()
PROJECT_CATALOG_LOCK = threading.RLock()
DB_LOCK = threading.Lock()

# 本机模型与快照路径（只读；模型文件本身不进版本库）。
MODEL_ROOT = SHARED_ROOT / "models" / "LLM"
PRIMARY_MODEL_ROOT = MODEL_ROOT / "Qwen3.8-27B-Uncensored"
PRIMARY_MODEL = PRIMARY_MODEL_ROOT / "Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
PRIMARY_MMPROJ = PRIMARY_MODEL_ROOT / "mmproj-F16.gguf"
FAST_MODEL_ROOT = MODEL_ROOT / "Qwen3-VL-8B-Instruct"
FAST_MODEL = FAST_MODEL_ROOT / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf"
FAST_MMPROJ = FAST_MODEL_ROOT / "mmproj-F16.gguf"
SNAPSHOT_ROOT = PROJECT / "workflows" / "snapshots"
RELEASE_BLOCKING_STATUSES = (
    "preparing",
    "submitting",
    "queued",
    "running",
    "stop_requested",
    "stopping",
    "batch_waiting",
    "scheduler_waiting",
    # A service restart leaves these tasks unresolved until they are checked
    # against ComfyUI history. Treat them as active so /free cannot unload a
    # model while an unknown task may still be running.
    "needs_reconcile",
)

# 作品注册表：作品清单是本机用户数据，干净检出时不存在。
PROJECTS_ROOT = PROJECT / "projects"
PROJECT_CATALOG_PATH = PROJECTS_ROOT / "catalog.json"
DIRECTOR_WORKSPACES_ROOT = STORAGE_ROOTS['workspaces']

# 其余按名字迭代的集合：改动必须同时更新对应契约。
# 作品专用的兜底覆盖绝不能泄漏到另一个作品清单。
PIPELINE_INPUT_OVERRIDES: dict[str, list[dict[str, Any]]] = {}
SEGMENT_GENERATION_FIELDS = (
    "duration_seconds", "location", "shot_size", "camera", "wardrobe",
    "performance", "prompt", "workflow", "keyframes", "audio", "reference_asset_id", "generation",
)
MATERIAL_EXTENSIONS = {
    "video": {".mp4", ".mov", ".webm", ".mkv", ".m4v"},
    "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "document": {".json", ".txt", ".md", ".srt", ".vtt", ".csv"},
}
