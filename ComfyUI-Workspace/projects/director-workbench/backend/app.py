from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import wave
import importlib.util
import logging
import psutil
from datetime import datetime, timezone
from functools import wraps
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, Request as BrowserRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, StrictBool, StrictInt, field_validator, model_validator
from . import production_presets, background_jobs, speech, keyframes, audio_edit_tasks, private_deletion, media_operations, gpu_idle_release, h3_duration, assembly_edit, guide, readiness, task_scheduler, creative_inspection, shot_creation
from .private_auth import current_principal
from .atomic_files import replace_prepared
from .storage import storage_roots, relocate_asset, legacy_mappings
from .user_context import project_location

PRIVATE_WORKSPACES = None

ROOT = Path(__file__).resolve().parents[4]
PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "ComfyUI-Workspace" / "runtime" / "director-workbench"
DB_PATH = RUNTIME / "director-workbench.sqlite3"
COMFY_URL = os.environ.get("DIRECTOR_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
RENDERER = ROOT / ".agents" / "skills" / "aigc-video-production" / "scripts" / "h3_render.py"
TEMPLATE = ROOT / ".agents" / "skills" / "aigc-video-production" / "assets" / "workflows" / "minimax-h3-current-api.json"
STORAGE_ROOTS = storage_roots(ROOT)
INPUT_ROOT = STORAGE_ROOTS['input']
OUTPUT_ROOT = STORAGE_ROOTS['output']
ASSEMBLY_BUILDER = PROJECT / "tools" / "build_assembly_workflow.py"
DIST = PROJECT / "dist"
REFERENCE_DECOMPOSER = PROJECT / "tools" / "reference_decompose.py"
REFERENCE_SEMANTIC_ANALYZER = PROJECT / "tools" / "reference_semantic.py"
REVERSE_DOCUMENT_LOCK = threading.RLock()
REVERSE_SEMANTIC_LOCK = threading.Lock()
GPU_ADMISSION_LOCK = threading.RLock()
REFERENCE_DECOMPOSE_ACTIVE = 0
MATERIAL_REUSE_LOCK = threading.RLock()
PLAN_EDIT_LOCK = threading.RLock()
AUDIO_EDIT_LOCK = threading.RLock()
MEDIA_OPERATION_LOCK = threading.RLock()
PROJECT_CATALOG_LOCK = threading.RLock()


def catalog_transaction(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with PROJECT_CATALOG_LOCK:
            return function(*args, **kwargs)
    return guarded


def plan_transaction(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with PLAN_EDIT_LOCK:
            return function(*args, **kwargs)
    return guarded


def write_plan_version(path: Path, document: dict[str, Any]) -> None:
    document['revision'] = int(document.get('revision', 0) or 0) + 1
    document['updated_at'] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    replace_prepared(temporary,path)


def check_plan_revision(document: dict[str, Any], expected: int | None) -> None:
    current = int(document.get('revision', 0) or 0)
    if expected is not None and expected != current:
        raise HTTPException(409, {'code': 'plan_revision_conflict', 'message': '分镜计划已被修改，请读取最新版本后合并草稿', 'expected_revision': expected, 'current_revision': current, 'retryable': False})


MODEL_ROOT = ROOT / "ComfyUI-Shared" / "models" / "LLM"
PRIMARY_MODEL_ROOT = MODEL_ROOT / "Qwen3.8-27B-Uncensored"
PRIMARY_MODEL = PRIMARY_MODEL_ROOT / "Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
PRIMARY_MMPROJ = PRIMARY_MODEL_ROOT / "mmproj-F16.gguf"
FAST_MODEL_ROOT = MODEL_ROOT / "Qwen3-VL-8B-Instruct"
FAST_MODEL = FAST_MODEL_ROOT / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf"
FAST_MMPROJ = FAST_MODEL_ROOT / "mmproj-F16.gguf"
PIPELINE_INPUT_CONTRACT_PATH = PROJECT / "contracts" / "pipeline-inputs.json"
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


def ensure_no_release_blocking_tasks(action: str, *, gpu_only: bool = False) -> None:
    """Prevent project mutations while any task may still own ComfyUI resources."""
    placeholders = ",".join("?" for _ in RELEASE_BLOCKING_STATUSES)
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            f"SELECT id,payload FROM tasks WHERE status IN ({placeholders})",
            RELEASE_BLOCKING_STATUSES,
        ).fetchall()
    active = any(not gpu_only or json.loads(row['payload']).get('kind') not in {'audio_edit', 'media_operation'} for row in rows)
    if active:
        raise HTTPException(409, f"仍有任务运行中，请先停止并等待任务结束后再{action}")
MEDIA: dict[str, Path] = {}
PIPELINE_STAGES: list[dict[str, Any]] = []

PIPELINE_INPUT_OVERRIDES: dict[str, list[dict[str, Any]]] = {}


def load_pipeline_input_contracts(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the small, editable JSON contract used by the operator UI.

    Keeping these controls in JSON means adding a script flag does not require
    a new React component or a backend route. A malformed/missing optional file
    falls back to the legacy Python metadata so the workbench remains usable.
    """
    try:
        document = json.loads((path or PIPELINE_INPUT_CONTRACT_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, dict):
        return {}
    return {
        str(stage_id): value
        for stage_id, value in stages.items()
        if isinstance(value, dict) and isinstance(value.get("inputs"), list)
    }


PIPELINE_INPUT_CONTRACTS = load_pipeline_input_contracts()


# The Python module is the platform adapter; all work-specific names, paths,
# stages and task recipes are loaded from a project manifest. Keeping one
# manifest is local user data, never a prerequisite of a clean source checkout.
PROJECTS_ROOT = PROJECT / "projects"
PROJECT_CATALOG_PATH = PROJECTS_ROOT / "catalog.json"
DIRECTOR_WORKSPACES_ROOT = STORAGE_ROOTS['workspaces']


def load_project_catalog() -> dict[str, Any]:
    try:
        document = json.loads(PROJECT_CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"projects": []}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取导演台项目目录: {PROJECT_CATALOG_PATH}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("projects"), list):
        raise RuntimeError("导演台项目目录格式无效：需要 projects 数组")
    return document


def load_project_manifest(project_id: str | None = None) -> dict[str, Any]:
    catalog = load_project_catalog()
    requested = project_id or os.environ.get("DIRECTOR_PROJECT_ID") or catalog.get("default_project_id")
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        PRIVATE_WORKSPACES.require(requested)
    entry = next((item for item in catalog["projects"] if isinstance(item, dict) and item.get("id") == requested), None)
    if entry is None:
        raise RuntimeError(f"导演台项目不存在: {requested}")
    manifest_path = PROJECTS_ROOT / str(entry.get("manifest", ""))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取项目配置: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("id") != requested:
        raise RuntimeError(f"项目配置与目录登记不一致: {manifest_path}")
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_manifest(manifest, ROOT)
    manifest["_manifest_path"] = str(manifest_path)
    return manifest


def registered_candidate(value: str | Path) -> Path:
    """Resolve storage aliases first; callers must still check ownership."""
    text = str(value).replace('\\', '/')
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = ROOT / text if text.startswith('ComfyUI-') else ROOT / 'ComfyUI-Shared' / text
    if not (ROOT / 'ComfyUI-Workspace/config/storage.json').is_file():
        return candidate
    return relocate_asset(candidate, ROOT, {
        'input': INPUT_ROOT, 'output': OUTPUT_ROOT, 'workspaces': DIRECTOR_WORKSPACES_ROOT,
        'private': PRIVATE_WORKSPACES.layout.root if PRIVATE_WORKSPACES else STORAGE_ROOTS['private'],
    })


def resolve_registered_path(value: str | Path) -> Path:
    """Resolve manifest paths from either the shared root or the workspace root."""
    candidate = registered_candidate(value)
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        return PRIVATE_WORKSPACES.file(candidate)
    resolved = candidate.resolve()
    allowed = [INPUT_ROOT.resolve(), OUTPUT_ROOT.resolve(), PROJECT.resolve(), DIRECTOR_WORKSPACES_ROOT.resolve()]
    allowed.extend(destination for _, destination in legacy_mappings(ROOT))
    if PRIVATE_WORKSPACES is not None:
        allowed.append(PRIVATE_WORKSPACES.layout.root)
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise ValueError("登记的素材路径不在允许的 ComfyUI 工作区内")
    return resolved


def shared_path(value: str | Path) -> Path:
    return resolve_registered_path(value)


def initial_project_manifest() -> dict[str, Any]:
    catalog = load_project_catalog()
    if catalog.get("default_project_id") or os.environ.get("DIRECTOR_PROJECT_ID"):
        return load_project_manifest()
    # No catalog, plan, work directory or sample media is created by importing.
    return {"id": "", "title": "", "plan_path": str(PROJECT / "runtime/unselected-plan.json")}


CURRENT_PROJECT = initial_project_manifest()
CURRENT_PROJECT_ID = str(CURRENT_PROJECT["id"])
PLAN_PATH = shared_path(str(CURRENT_PROJECT["plan_path"]))
ASSEMBLY_CONFIG = dict(CURRENT_PROJECT.get("assembly") or {})
ASSEMBLY_BUILDER = PROJECT / str(ASSEMBLY_CONFIG.get("builder", "tools/build_assembly_workflow.py"))
PIPELINE_INPUT_CONTRACT_PATH = PROJECT / str(CURRENT_PROJECT.get("pipeline_contract", "contracts/pipeline-inputs.json"))
PIPELINE_INPUT_CONTRACTS = load_pipeline_input_contracts()
PIPELINE_STAGES = list(CURRENT_PROJECT.get("pipeline", []))
MEDIA = {str(alias): shared_path(path) for alias, path in (CURRENT_PROJECT.get("media") or {}).items()}
TASK_TEMPLATES = {str(key): dict(value) for key, value in (CURRENT_PROJECT.get("task_templates") or {}).items() if isinstance(value, dict)}
# Work-specific fallback overrides must never leak into another manifest.
PIPELINE_INPUT_OVERRIDES = {}


def apply_project_manifest(manifest: dict[str, Any]) -> None:
    """Switch the active project without changing the renderer or storage roots."""
    global CURRENT_PROJECT, CURRENT_PROJECT_ID, PLAN_PATH, ASSEMBLY_CONFIG
    global ASSEMBLY_BUILDER, PIPELINE_INPUT_CONTRACT_PATH, PIPELINE_INPUT_CONTRACTS
    global PIPELINE_STAGES, MEDIA, TASK_TEMPLATES
    CURRENT_PROJECT = manifest
    CURRENT_PROJECT_ID = str(manifest["id"])
    PLAN_PATH = shared_path(str(manifest["plan_path"]))
    ASSEMBLY_CONFIG = dict(manifest.get("assembly") or {})
    ASSEMBLY_BUILDER = PROJECT / str(ASSEMBLY_CONFIG.get("builder", "tools/build_assembly_workflow.py"))
    PIPELINE_INPUT_CONTRACT_PATH = PROJECT / str(manifest.get("pipeline_contract", "contracts/pipeline-inputs.json"))
    PIPELINE_INPUT_CONTRACTS = load_pipeline_input_contracts()
    PIPELINE_STAGES = list(manifest.get("pipeline", []))
    MEDIA = {str(alias): shared_path(path) for alias, path in (manifest.get("media") or {}).items()}
    TASK_TEMPLATES = {str(key): dict(value) for key, value in (manifest.get("task_templates") or {}).items() if isinstance(value, dict)}


def public_project(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    value = dict(manifest or CURRENT_PROJECT)
    value.pop("_manifest_path", None)
    # Keep the operator view honest after an external cleanup: stale file
    # records should not turn into broken preview cards.
    media = value.get("media")
    if isinstance(media, dict):
        value["media"] = {alias: path for alias, path in media.items() if _registered_file_exists(path)}
    assets = value.get("assets")
    if isinstance(assets, list):
        value["assets"] = [asset for asset in assets if not isinstance(asset, dict) or _asset_record_exists(asset)]
    return value


def _registered_file_exists(value: Any) -> bool:
    try:
        return resolve_registered_path(str(value)).is_file()
    except (ValueError, OSError):
        return False


def _asset_record_exists(asset: dict[str, Any]) -> bool:
    paths: list[Any] = []
    if asset.get("image_path"):
        paths.append(asset["image_path"])
    sources = asset.get("sources")
    if isinstance(sources, dict):
        paths.extend(sources.values())
    return not paths or any(_registered_file_exists(path) for path in paths)


def root_relative_path(path: Path) -> str:
    """Return a portable path understood by the shared media endpoint."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        allowed = [INPUT_ROOT.resolve(), OUTPUT_ROOT.resolve(), DIRECTOR_WORKSPACES_ROOT.resolve()]
        allowed.extend(destination for _, destination in legacy_mappings(ROOT))
        if PRIVATE_WORKSPACES is not None:
            allowed.append(PRIVATE_WORKSPACES.layout.root)
        if any(resolved.is_relative_to(root) for root in allowed):
            return resolved.as_posix()
        raise ValueError(f"路径必须位于配置的作品存储目录内: {path}") from exc


def project_entries() -> list[dict[str, Any]]:
    return [item for item in load_project_catalog().get("projects", []) if isinstance(item, dict)]


def unique_project_id(requested: str, existing: set[str] | None = None) -> str:
    value = "".join(char if char.isalnum() or char in "-_" else "-" for char in requested).strip("-")[:80] or "new-project"
    known = existing or {str(item.get("id")) for item in project_entries()}
    if value not in known:
        return value
    suffix = 2
    while f"{value}-{suffix}" in known:
        suffix += 1
    return f"{value}-{suffix}"


@catalog_transaction
def persist_project_manifest(manifest: dict[str, Any], make_default: bool = True) -> None:
    project_id = str(manifest["id"])
    manifest_path = PROJECTS_ROOT / f"{project_id}.json"
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog = load_project_catalog()
    entries = [item for item in catalog.get("projects", []) if isinstance(item, dict) and item.get("id") != project_id]
    entries.append({"id": project_id, "manifest": manifest_path.name})
    catalog["projects"] = entries
    if make_default:
        catalog["default_project_id"] = project_id
    PROJECT_CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def blank_project_manifest(project_id: str, series: str, title: str, workspace: Path) -> dict[str, Any]:
    plan_path = workspace / "plans" / "plan.json"
    return {
        "schema_version": 1, "id": project_id, "series": series.strip() or "未命名系列", "title": title.strip() or "未命名作品",
        "tag": "新建项目", "eyebrow": "导演工作台 / 新作品", "subtitle": "从资源登记和制作步骤开始规划。",
        "workspace_root": root_relative_path(workspace), "plan_path": root_relative_path(plan_path), "pipeline_contract": "contracts/pipeline-inputs.json",
        "assembly_asset_id": "MASTER", "default_task_asset_id": "", "media": {}, "assets": [], "pipeline": [], "task_templates": {},
        "assembly": {"filename_prefix": f"导演工作台工作目录/{project_id}/assembly"},
    }


def safe_material_label(value: str) -> str:
    return ''.join(char for char in value if unicodedata.category(char)[0] != 'C' and char not in '<>:"/\\|?*').strip(' .')[:160]


def imported_asset_records(media: dict[str, str], original_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alias, value in media.items():
        suffix = Path(alias).suffix.lower()
        kind = "video" if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"} else "audio" if suffix in {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"} else "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else ""
        if not kind:
            continue
        stem = Path(alias).stem
        asset_id = "asset-" + "".join(char.lower() if char.isalnum() else "-" for char in stem).strip("-")[:48]
        if not asset_id or asset_id in seen:
            asset_id = f"asset-{len(records) + 1:03d}"
        seen.add(asset_id)
        original_name = (original_names or {}).get(alias)
        item: dict[str, Any] = {"id": asset_id, "kind": kind, "title": original_name or stem or alias, "subtitle": "已登记资源", "ready": True, "origin": "工作目录导入", "intent": "", "prompt": "", "specs": suffix.lstrip(".").upper(), "workflow": "待登记"}
        if original_name:
            item.update(original_filename=original_name, origin='本机上传')
        if kind == "image":
            item["image_path"] = value
        else:
            item["sources"] = {"A": value}
        records.append(item)
    return records


def project_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    value = public_project(manifest)
    assets = value.get("assets") if isinstance(value.get("assets"), list) else []
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    return {
        "id": value.get("id"), "series": value.get("series", ""), "title": value.get("title", value.get("id", "")),
        "subtitle": value.get("subtitle", ""), "tag": value.get("tag", ""),
        "asset_count": len(assets) or len(media), "pipeline_count": len(value.get("pipeline", [])) if isinstance(value.get("pipeline"), list) else 0,
    }


def scan_import_directory(source: Path, project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse a director workspace without requiring a project-specific script."""
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("导入路径必须是目录")
    manifest_file = next((source / name for name in ("director.project.json", "project.json", ".director-workbench/project.json") if (source / name).is_file()), None)
    source_files = [item for item in source.rglob("*") if item.is_file() and ".git" not in item.parts and "node_modules" not in item.parts]
    by_kind = {
        "video": {".mp4", ".mov", ".webm", ".mkv"}, "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac"},
        "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"}, "workflow": {".json"}, "prompt": {".txt", ".md"},
    }
    found = {kind: [item for item in source_files if item.suffix.lower() in suffixes] for kind, suffixes in by_kind.items()}
    plan_files: list[Path] = []
    for item in found["workflow"]:
        try:
            document = json.loads(item.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and isinstance(document.get("segments"), list):
            plan_files.append(item)
    manifest: dict[str, Any] = {}
    if manifest_file:
        try:
            loaded = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"项目清单无法读取: {manifest_file}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("项目清单必须是 JSON 对象")
        manifest.update(loaded)
    manifest.update({"schema_version": int(manifest.get("schema_version", 1)), "id": project_id})
    manifest.setdefault("series", source.parent.name if source.parent.name else "未分类系列")
    manifest.setdefault("title", source.name)
    manifest.setdefault("subtitle", "从现有工作目录导入")
    manifest.setdefault("tag", "已导入项目")
    manifest.setdefault("eyebrow", "导演工作台 / 导入项目")
    manifest.setdefault("workspace_root", root_relative_path(source))
    if plan_files:
        manifest.setdefault("plan_path", root_relative_path(plan_files[0]))
    elif not manifest.get("plan_path"):
        manifest["plan_path"] = ""
    if not isinstance(manifest.get("media"), dict):
        manifest["media"] = {}
    if not isinstance(manifest.get("assets"), list):
        manifest["assets"] = []
    known_paths = {str(value).replace("\\", "/") for value in manifest["media"].values()}
    for item in source_files:
        if item.suffix.lower() not in by_kind["video"] | by_kind["audio"] | by_kind["image"]:
            continue
        relative = root_relative_path(item)
        if relative in known_paths:
            continue
        alias = item.name
        if alias in manifest["media"]:
            alias = f"{item.stem}-{len(manifest['media'])}{item.suffix}"
        manifest["media"][alias] = relative
    if not manifest["assets"]:
        manifest["assets"] = imported_asset_records(manifest["media"])
    report = {
        "source": root_relative_path(source), "manifest_found": bool(manifest_file), "manifest_file": root_relative_path(manifest_file) if manifest_file else None,
        "plan_files": [root_relative_path(item) for item in plan_files],
        "files": {kind: [root_relative_path(item) for item in values] for kind, values in found.items()},
        "counts": {kind: len(values) for kind, values in found.items()},
        "unclassified": [root_relative_path(item) for item in source_files if all(item.suffix.lower() not in suffixes for suffixes in by_kind.values())],
    }
    return manifest, report

app = FastAPI(title="Director Workbench API", version="0.1.0")


@app.get('/api/auth/config')
def authentication_config():
    return {'enabled': PRIVATE_WORKSPACES is not None}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"],
    allow_origin_regex=r"^https?://(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|100\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)
DB_LOCK = threading.Lock()


def db() -> sqlite3.Connection:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL,
        prompt_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        stop_requested INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
        result TEXT, error TEXT, batch_id TEXT, sequence INTEGER, project_id TEXT
    )""")
    # Existing pilot databases predate batch metadata; keep them readable.
    for column, declaration in (("batch_id", "TEXT"), ("sequence", "INTEGER"), ("project_id", "TEXT")):
        try:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError:
            pass
    connection.execute("""CREATE TABLE IF NOT EXISTS resource_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, timestamp REAL NOT NULL,
        gpu_free_mib INTEGER, gpu_total_mib INTEGER, ram_free_gib REAL,
        queue_running INTEGER, queue_pending INTEGER
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS project_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        asset_id TEXT NOT NULL DEFAULT '',
        record_type TEXT NOT NULL,
        revision INTEGER NOT NULL,
        source TEXT NOT NULL,
        created_at REAL NOT NULL,
        data TEXT NOT NULL,
        UNIQUE(project_id, asset_id, record_type, revision)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_asset ON tasks(project_id, asset_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_records_project_type ON project_records(project_id, record_type, asset_id, revision DESC)")
    connection.execute("CREATE TABLE IF NOT EXISTS user_settings (owner TEXT PRIMARY KEY, revision INTEGER NOT NULL, guide TEXT NOT NULL)")
    connection.commit()
    return connection


def request_json(path: str, payload: dict[str, Any] | None = None, timeout: float = 8) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(COMFY_URL + path, data=data, method="POST" if data else "GET", headers={"Content-Type": "application/json"} if data else {})
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def comfy_status() -> dict[str, Any]:
    try:
        stats = request_json("/system_stats")
        queue = request_json("/queue")
        system = stats.get("system", {})
        device = (stats.get("devices") or [{}])[0]
        return {
            "online": True, "url": COMFY_URL, "version": system.get("comfyui_version"),
            "queue_running": len(queue.get("queue_running", [])),
            "queue_pending": len(queue.get("queue_pending", [])),
            "gpu_free_mib": round(device.get("vram_free", 0) / 2**20),
            "gpu_total_mib": round(device.get("vram_total", 0) / 2**20),
            "ram_free_gib": round(system.get("ram_free", 0) / 2**30, 2),
        }
    except (OSError, ValueError, HTTPError, URLError) as exc:
        return {"online": False, "url": COMFY_URL, "error": str(exc)}


def gpu_admission(function):
    """Reserve admission until a GPU request is represented in task state."""
    @wraps(function)
    def guarded(*args, **kwargs):
        # CPU audio task control must remain available while the GPU is occupied.
        task_id = kwargs.get('task_id')
        if task_id is None and function.__name__ in {'resume', 'resume_project_task', 'reconcile_project_task', 'resolve_missing_execution'}:
            task_id = args[0 if function.__name__ == 'resume' else 1] if len(args) > (0 if function.__name__ == 'resume' else 1) else None
        if task_id and (get_task(task_id) or {}).get('payload', {}).get('kind') in {'audio_edit', 'media_operation'}:
            return function(*args, **kwargs)
        with GPU_ADMISSION_LOCK:
            GPU_IDLE_RELEASER.note_activity()
            return function(*args, **kwargs)
    return guarded


def analysis_job_root() -> Path:
    return DB_PATH.parent / "reference-jobs"


def ensure_no_unresolved_analysis_jobs() -> None:
    with background_jobs.LOCK:
        if any(job["status"] == "needs_reconcile" for job in background_jobs.records(analysis_job_root())):
            raise HTTPException(409, "有参考分析在服务重启后待核对，请先确认原分析进程与结果")


def require_submission_capacity() -> dict[str, Any]:
    """Admission validates queue capacity; resources are checked at dispatch."""
    principal = current_principal.get()
    owner = principal.identity.username if principal else '__legacy__'
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, owner)
    return {'accepted_for_queue': True}


def task_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = json.loads(value["payload"])
    value["result"] = json.loads(value["result"]) if value["result"] else None
    value["stop_requested"] = bool(value["stop_requested"])
    return value


def set_task(task_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with DB_LOCK, db() as connection:
        connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*fields.values(), task_id])
        connection.commit()


def sample_resources(task_id: str) -> None:
    info = comfy_status()
    if not info.get("online"):
        return
    with DB_LOCK, db() as connection:
        connection.execute("INSERT INTO resource_samples (task_id, timestamp, gpu_free_mib, gpu_total_mib, ram_free_gib, queue_running, queue_pending) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, time.time(), info.get("gpu_free_mib"), info.get("gpu_total_mib"), info.get("ram_free_gib"), info.get("queue_running"), info.get("queue_pending")))
        connection.commit()


def get_task(task_id: str) -> dict[str, Any] | None:
    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    task = task_row(row) if row else None
    if task and PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_task(task)
    if task:
        task['queue'] = TASK_SCHEDULER.queue_info(task_id)
    return task


def task_snapshot_root(project_id: str) -> Path:
    private = PRIVATE_WORKSPACES.snapshots(project_id) if PRIVATE_WORKSPACES is not None else None
    return private or SNAPSHOT_ROOT


def publish_task_outputs(task: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is None or PRIVATE_WORKSPACES.verify_task(task) is None:
        return done
    outputs = [PRIVATE_WORKSPACES.publish(task, OUTPUT_ROOT / str(value), OUTPUT_ROOT, index)
               for index, value in enumerate(done.get('outputs', []))]
    return {**done, 'published_outputs': [root_relative_path(path) for path in outputs]}


def get_current_project_task(task_id: str) -> dict[str, Any] | None:
    task = get_task(task_id)
    return task if task and task.get("project_id") == CURRENT_PROJECT_ID else None


def record_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["data"] = json.loads(value["data"])
    value["created_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value["created_at"]))
    return value


def append_project_record(
    record_type: str,
    data: dict[str, Any],
    *,
    asset_id: str = "",
    source: str = "director-ui",
    project_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    target_project = project_id or CURRENT_PROJECT_ID
    now = time.time()
    with DB_LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_revision = int(connection.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM project_records WHERE project_id = ? AND asset_id = ? AND record_type = ?",
            (target_project, asset_id, record_type),
        ).fetchone()[0])
        if expected_revision is not None and expected_revision != current_revision:
            raise HTTPException(409, {"code": "record_revision_conflict", "message": "记录已更新，请读取最新版本后再保存；当前编辑未覆盖服务器记录", "current_revision": current_revision, "expected_revision": expected_revision, "retryable": False})
        row = append_project_record_in_connection(connection, target_project, record_type, data, asset_id, source, now)
        connection.commit()
    return record_row(row)


def append_project_record_in_connection(
    connection: sqlite3.Connection,
    project_id: str,
    record_type: str,
    data: dict[str, Any],
    asset_id: str = "",
    source: str = "director-ui",
    created_at: float | None = None,
) -> sqlite3.Row:
    """Append a record using an existing transaction."""
    timestamp = created_at if created_at is not None else time.time()
    previous = connection.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM project_records WHERE project_id = ? AND asset_id = ? AND record_type = ?",
        (project_id, asset_id, record_type),
    ).fetchone()[0]
    revision = int(previous) + 1
    cursor = connection.execute(
        "INSERT INTO project_records (project_id, asset_id, record_type, revision, source, created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, asset_id, record_type, revision, source, timestamp, json.dumps(data, ensure_ascii=False)),
    )
    row = connection.execute("SELECT * FROM project_records WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if row is None:
        raise RuntimeError("项目记录写入后无法读取")
    return row


def project_records(project_id: str | None = None) -> list[dict[str, Any]]:
    target_project = project_id or CURRENT_PROJECT_ID
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT * FROM project_records WHERE project_id = ? ORDER BY created_at, id",
            (target_project,),
        ).fetchall()
    return [record_row(row) for row in rows]


def resolve_workflow_path(value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raw = str(value).replace("\\", "/")
        candidate = ROOT / raw if raw.startswith("ComfyUI-Workspace/") else PROJECT / raw
    candidate = registered_candidate(candidate)
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        resolved = PRIVATE_WORKSPACES.file(candidate)
        if resolved.suffix.lower() != '.json' or not resolved.is_file():
            raise ValueError('工作流 JSON 不存在')
        return resolved
    resolved = candidate.resolve()
    if PRIVATE_WORKSPACES is not None and resolved.is_relative_to(PRIVATE_WORKSPACES.layout.root):
        if resolved.suffix.lower() != '.json' or not resolved.is_file():
            raise ValueError('工作流 JSON 不存在')
        return resolved
    workflow_roots = [PROJECT.resolve(), DIRECTOR_WORKSPACES_ROOT.resolve()]
    workflow_roots.extend(destination for source, destination in legacy_mappings(ROOT) if source == ROOT / 'ComfyUI-Workspace/projects/director-workbench/workspaces')
    if not any(resolved.is_relative_to(root) for root in workflow_roots):
        raise ValueError("工作流必须位于导演台项目或作品目录内")
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        raise ValueError(f"工作流 JSON 不存在: {resolved}")
    return resolved


def apply_prompt_override(graph: dict[str, Any], prompt: str, node_ids: list[str] | None = None) -> list[str]:
    requested = {str(value) for value in (node_ids or [])}
    candidates: list[str] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        if "prompt" not in node["inputs"] or not isinstance(node["inputs"]["prompt"], str):
            continue
        if requested and str(node_id) not in requested:
            continue
        candidates.append(str(node_id))
    if requested and set(candidates) != requested:
        missing = sorted(requested - set(candidates))
        raise ValueError(f"工作流声明的提示词节点不可覆盖: {', '.join(missing)}")
    if not candidates:
        raise ValueError("该工作流没有可确认的提示词输入，已禁止静默忽略提示词编辑")
    if not requested and len(candidates) > 1:
        raise ValueError("工作流包含多个提示词输入；请在任务模板中登记 prompt_node_ids 后再编辑")
    for node_id in candidates:
        graph[node_id]["inputs"]["prompt"] = prompt
    return candidates


def apply_graph_bindings(graph: dict[str, Any], bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply declared scalar overrides and return an auditable change list."""
    applied: list[dict[str, Any]] = []
    for binding in bindings:
        node_id = str(binding.get("node_id", ""))
        input_name = str(binding.get("input", ""))
        node = graph.get(node_id)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"执行绑定节点不存在: {node_id}")
        if input_name not in node["inputs"]:
            raise ValueError(f"执行绑定输入不存在: {node_id}.{input_name}")
        previous = node["inputs"][input_name]
        if isinstance(previous, list):
            raise ValueError(f"执行绑定只能覆盖标量输入，不能改写连接: {node_id}.{input_name}")
        value = binding.get("value")
        node["inputs"][input_name] = value
        applied.append({
            "input_id": str(binding.get("input_id", "")),
            "node_id": node_id,
            "input": input_name,
            "previous": previous,
            "value": value,
        })
    return applied


def resolve_pipeline_material(value: Any, kind: str, manifest: dict[str, Any] | None = None) -> Any:
    if kind not in {"图片", "音频", "视频"}:
        return value
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, '制作素材必须是已登记素材的标识或可授权读取的文件引用')
    selected: Any = value
    assets = (manifest if manifest is not None else CURRENT_PROJECT).get("assets")
    if isinstance(assets, list):
        record = next((item for item in assets if isinstance(item, dict) and str(item.get("id")) == value), None)
        if record:
            if kind == "图片":
                selected = record.get("image_path")
            else:
                sources = record.get("sources")
                selected = sources.get("A") if isinstance(sources, dict) else None
            if not selected:
                raise HTTPException(422, f"素材 {value} 没有可执行的{kind}文件")
    try:
        resolved = resolve_registered_path(str(selected))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f"素材路径不可用于执行: {selected}: {exc}") from exc
    if not resolved.is_file():
        raise HTTPException(422, f"素材文件不存在: {resolved}")
    return str(resolved)


def prepare_pipeline_payload(payload: dict[str, Any], stage_id: str, values: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve, validate and bind one stage's operator inputs before freezing."""
    project = manifest if manifest is not None else CURRENT_PROJECT
    stages = project.get('pipeline', []) if manifest is not None else PIPELINE_STAGES
    stage = next((item for item in stages if str(item.get("id")) == stage_id), None)
    if stage is None:
        raise HTTPException(422, f"当前项目没有制作步骤: {stage_id}")
    contract = pipeline_contract(stage, manifest)
    execution = contract.get("execution") if isinstance(contract.get("execution"), dict) else {}
    if execution.get("mode") != "comfyui":
        raise HTTPException(422, f"“{stage.get('title', stage_id)}”尚未登记可提交的 ComfyUI 适配器")
    if not payload.get("workflow"):
        raise HTTPException(422, f"{payload.get('asset_id', '当前片段')} 没有可绑定的 ComfyUI API 图")
    try:
        source = resolve_workflow_path(str(payload["workflow"]))
        graph = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"无法读取待绑定的 ComfyUI API 图: {exc}") from exc
    if not isinstance(graph, dict):
        raise HTTPException(422, "ComfyUI API 工作流必须是 JSON 对象")

    effective = dict(values)
    preset = project.get("production_preset") or {}
    graph_preset_id = (graph.get("92", {}).get("_meta") or {}).get("director_preset")
    h3_preset_id = next((key for key in production_presets.PRESET_IDS
                         if stage_id == production_presets.stage_id(key)
                         and (graph_preset_id == key or str(stage.get('recipe', '')).startswith('H3 '))), None)
    if h3_preset_id:
        expected = production_presets.load_recipe(h3_preset_id)
        if h3_preset_id in production_presets.TEXT_PRESET_IDS and (
            any(node.get('class_type') in {'LoadImage', 'LoadAudio'} for node in graph.values() if isinstance(node, dict))
            or any(name in graph.get('105:104', {}).get('inputs', {}) for name in ('first_frame', 'last_frame'))
        ):
            raise HTTPException(422, '纯文本方案不应包含首尾帧或外部音频输入，请重新配置制作方案')
        fixed_inputs = {'115': ('aspect_ratio', 'megapixels', 'multiple'),
                        '105:107': ('expression', 'values.a'),
                        '105:104': ('width', 'height', 'length', 'first_frame', 'clip', 'vae'),
                        '105:111': (), '105:11': ('vae_name',),
                        '105:91': ('fps',)}
        if graph_preset_id != h3_preset_id or any(
            not isinstance(graph.get(node_id), dict) or
            graph[node_id].get('class_type') != expected[node_id]['class_type'] or
            any(graph[node_id].get('inputs', {}).get(name) != expected[node_id]['inputs'].get(name) for name in names)
            for node_id, names in fixed_inputs.items()
        ):
            raise HTTPException(422, 'H3 工作流的帧网格、画幅或帧率已改变，请重新配置制作方案')
    if preset.get("workflow") == payload.get("workflow") or graph_preset_id in production_presets.PRESET_IDS:
        document, _ = load_project_plan(str(project['id']) if manifest is not None else CURRENT_PROJECT_ID)
        segment = next((item for item in document.get("segments", []) if item.get("id") == payload.get("asset_id")), None)
        if segment is None:
            raise HTTPException(422, "请先保存分镜，再配置生成参数")
        if (segment.get('generation') or {}).get('mode') == 'auto':
            _, selected_stage = select_preset_execution(project, segment.get('workflow'), None)
            if stage_id != selected_stage:
                raise HTTPException(422, '自动创作方式由已保存输入决定；请预检当前创作输入。')
            capability = production_presets.catalog(ROOT / 'ComfyUI-Shared' / 'models', h3_preset_id)
            if not capability.get('available') or capability.get('missing_models'):
                raise HTTPException(422, {'code': 'creation_resources_missing',
                    'message': '当前创作所需模型或制作文件不可用，请联系管理员补齐；分镜已保存，不会自动下载。',
                    'missing_models': capability.get('missing_models', []),
                    'blockers': [{'code': 'creation_resources_missing', 'message': '所需制作资源不可用，请联系管理员补齐后重试。', 'action': 'contact_administrator'}]})
        defaults = {"first-frame": (segment.get("keyframes") or {}).get("first"), "last-frame": (segment.get("keyframes") or {}).get("last"),
                    "guide": (segment.get("audio") or {}).get("guide"), "prompt": segment.get("prompt"),
                    "duration": segment.get("duration_seconds"), "seed": (segment.get('generation') or {}).get('seed', 42), "turbo-sampling": "enable"}
        accepted_inputs = {spec.get('id') for spec in contract.get('input_specs', [])}
        for key, value in defaults.items():
            if key in accepted_inputs and key not in effective and value is not None:
                effective[key] = value
    specs = contract.get("input_specs") if isinstance(contract.get("input_specs"), list) else []
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("id") in effective:
            continue
        binding = spec.get("binding")
        if not isinstance(binding, dict):
            continue
        node = graph.get(str(binding.get("node_id", "")))
        node_inputs = node.get("inputs") if isinstance(node, dict) else None
        input_name = str(binding.get("input", ""))
        if not isinstance(node_inputs, dict) or input_name not in node_inputs:
            continue
        current = node_inputs[input_name]
        value_map = binding.get("value_map")
        if isinstance(value_map, dict):
            current = next((key for key, mapped in value_map.items() if mapped == current), current)
        effective[str(spec["id"])] = current

    validation = validate_pipeline_values(stage, effective, manifest, collect_errors=True)
    normalized = dict(validation["values"])
    input_blockers = list(validation.get('blockers') or [])
    if (preset.get("workflow") == payload.get("workflow") or graph_preset_id in production_presets.PRESET_IDS) and 'duration' in normalized and float(normalized['duration']) != float(segment.get('duration_seconds', 0)):
        input_blockers.append({'code': 'duration_not_saved', 'message': '请先保存分镜时长', 'input_id': 'duration', 'action': 'save_shot'})
    material_blockers = []
    for spec in specs:
        key = str(spec.get('id', ''))
        if key in normalized and spec.get('kind') in {'图片', '音频', '视频'}:
            try:
                normalized[key] = resolve_pipeline_material(normalized[key], str(spec['kind']), manifest)
            except (HTTPException, OSError, ValueError):
                material_blockers.append({'code': 'material_unavailable', 'message': f"{spec.get('label', key)} 未登记或无法读取，请重新选择素材", 'input_id': key, 'action': 'upload_material'})
    if input_blockers or material_blockers:
        code = 'invalid_inputs' if input_blockers else 'invalid_materials'
        if len(input_blockers) == 1 and input_blockers[0]['code'] == 'duration_not_saved' and not material_blockers:
            code = 'duration_not_saved'
        raise HTTPException(422, {'code': code, 'message': '请补齐或修正制作输入与素材', 'blockers': input_blockers + material_blockers})
    if h3_preset_id:
        aspect = production_presets.aspect_ratio(h3_preset_id)
        duration_profile = h3_duration.plan(normalized.get('duration'), aspect)
    bindings: list[dict[str, Any]] = []
    parameters: dict[str, Any] = {}
    materials: dict[str, Any] = {}
    executed_prompt: str | None = None
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        input_id = str(spec.get("id", ""))
        if input_id not in normalized:
            continue
        value = resolve_pipeline_material(normalized[input_id], str(spec.get("kind", "参数")), manifest)
        normalized[input_id] = value
        binding = spec.get("binding")
        if not isinstance(binding, dict):
            raise HTTPException(422, f"输入“{spec.get('label', input_id)}”尚未登记执行绑定，已阻止静默忽略")
        value_map = binding.get("value_map")
        mapped = value_map.get(str(value), value) if isinstance(value_map, dict) else value
        bindings.append({"input_id": input_id, "node_id": str(binding.get("node_id", "")), "input": str(binding.get("input", "")), "value": mapped})
        kind = str(spec.get("kind", "参数"))
        if kind in {"图片", "音频", "视频"}:
            materials[input_id] = value
        elif input_id == "prompt":
            executed_prompt = str(value)
        else:
            parameters[input_id] = value

    prepared = dict(payload)
    if 'segment' in locals() and isinstance(segment, dict):
        prepared['segment_dependencies'] = list(segment.get('dependencies') or [])
    prepared["pipeline_stage_id"] = stage_id
    prepared["pipeline_values"] = normalized
    prepared["parameters"] = parameters
    prepared["materials"] = materials
    prepared["executed_prompt"] = executed_prompt
    if 'segment' in locals() and segment.get('resolved_generation'):
        prepared['resolved_generation'] = segment['resolved_generation']
    prepared["_graph_bindings"] = bindings
    if h3_preset_id:
        prepared['h3_duration'] = {
            **duration_profile, 'generation_mode': 'text_to_video' if h3_preset_id in production_presets.TEXT_PRESET_IDS else 'image_to_video',
            'first_frame': materials.get('first-frame'), 'first_frame_node_id': '114' if 'first-frame' in materials else None,
            'prompt': executed_prompt, 'prompt_node_id': '105:104', 'duration_node_id': '105:111',
        }
    if 'segment' in locals() and isinstance(segment, dict):
        prepared['creative_inspection'] = inspect_shot(project, document, segment, prepared.get('h3_duration'))
    return prepared


def stage_comfy_input_files(graph: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    """Freeze input media under ComfyUI's input root and bind relative names."""
    project_folder = "".join(char if char.isalnum() or char in "-_" else "-" for char in project_id)
    attempt = uuid.uuid4().hex
    staged: list[dict[str, Any]] = []
    for node_id, node in graph.items():
        field = {"LoadImage": "image", "LoadAudio": "audio", "LoadVideo": "file"}.get(node.get("class_type"))
        value = node.get("inputs", {}).get(field) if field else None
        if not isinstance(value, str) or not value:
            continue
        normalized = value.replace("\\", "/")
        if Path(value).is_absolute() or normalized.startswith(("ComfyUI-", "input/", "output/")):
            source = resolve_registered_path(value)
        else:
            base = OUTPUT_ROOT if normalized.endswith(" [output]") else INPUT_ROOT
            relative = normalized.removesuffix(" [input]").removesuffix(" [output]")
            source = (base / relative).resolve()
            if not source.is_relative_to(base.resolve()):
                raise ValueError("ComfyUI 输入素材路径越界")
            if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
                source = PRIVATE_WORKSPACES.file(source)
        if not source.is_file():
            raise ValueError(f"ComfyUI 输入素材不存在: {source}")
        destination = INPUT_ROOT / "导演工作台工作目录" / project_folder / "任务素材" / attempt / f"{len(staged) + 1:02d}{source.suffix.lower()}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        shutil.copy2(source, destination)
        after = source.stat()
        if destination.stat().st_size != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise ValueError("素材复制时发生变化，请重新提交")
        reference = destination.relative_to(INPUT_ROOT).as_posix()
        node["inputs"][field] = reference
        staged.append({"node_id": node_id, "input": field, "source": str(source), "path": str(destination), "reference": reference, "size_bytes": before.st_size})
    return staged


def freeze_task_payload(payload: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    """Freeze the exact inputs and API graph before a task enters the queue."""
    frozen = json.loads(json.dumps(payload, ensure_ascii=False))
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.freeze_owner(frozen, project_id)
    if frozen.get("workflow"):
        source = resolve_workflow_path(str(frozen["workflow"]))
        graph = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(graph, dict):
            raise ValueError("ComfyUI API 工作流必须是 JSON 对象")
        prompt_override = frozen.pop("prompt_override", None)
        executed_prompt = frozen.pop("executed_prompt", None)
        graph_bindings = frozen.pop("_graph_bindings", [])
        prompt_nodes: list[str] = []
        if prompt_override is not None:
            prompt_nodes = apply_prompt_override(graph, str(prompt_override), frozen.get("prompt_node_ids"))
        applied_bindings = apply_graph_bindings(graph, graph_bindings) if graph_bindings else []
        staged_materials = stage_comfy_input_files(graph, project_id)
        for binding in applied_bindings:
            staged = next((item for item in staged_materials if item["node_id"] == binding["node_id"] and item["input"] == binding["input"]), None)
            if staged:
                binding["source_value"] = binding["value"]
                binding["value"] = staged["reference"]
        if (graph.get("92", {}).get("_meta") or {}).get("director_preset") in production_presets.PRESET_IDS:
            asset_name = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(frozen.get("asset_id", "shot")))
            graph["92"]["inputs"]["filename_prefix"] = f"导演工作台工作目录/{project_id}/video/{asset_name}-{uuid.uuid4().hex[:10]}"
        snapshot_root = task_snapshot_root(project_id)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        destination = snapshot_root / f"api-{uuid.uuid4().hex[:16]}.json"
        destination.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        frozen["source_workflow"] = str(source)
        frozen["workflow"] = str(destination)
        frozen["execution_snapshot"] = {
            "api_graph": str(destination),
            "source_workflow": str(source),
            "prompt": str(executed_prompt) if executed_prompt is not None else str(prompt_override) if prompt_override is not None else None,
            "prompt_node_ids": prompt_nodes,
            "parameters": dict(frozen.get("parameters") or {}),
            "materials": dict(frozen.get("materials") or {}),
            "h3_duration": frozen.get("h3_duration"),
            "resolved_generation": frozen.get("resolved_generation"),
            "creative_inspection": frozen.get("creative_inspection"),
            "pipeline_stage_id": frozen.get("pipeline_stage_id"),
            "applied_bindings": applied_bindings,
            "staged_materials": staged_materials,
            "frozen_at": time.time(),
        }
        return frozen

    if not frozen.get("prompt"):
        raise ValueError("任务模板缺少提示词")
    recipe_path = write_recipe_snapshot(frozen, project_id=project_id)
    if frozen.get('owner_user'):
        frozen['workflow'] = recipe_path
    frozen["execution_snapshot"] = {
        "api_graph": recipe_path,
        "source_workflow": str(TEMPLATE),
        "prompt": frozen["prompt"],
        "prompt_node_ids": [],
        "parameters": {"seed": frozen.get("seed"), "duration_seconds": 5, "aspect_ratio": "9:16"},
        "materials": {key: frozen.get(key) for key in ("first_frame", "last_frame", "audio_guide")},
        "frozen_at": time.time(),
    }
    return frozen


def history_done(prompt_id: str) -> dict[str, Any] | None:
    try:
        history = request_json(f"/history/{prompt_id}")
    except (OSError, ValueError, HTTPError, URLError):
        return None
    entry = history.get(prompt_id)
    if not entry:
        return None
    status = entry.get("status", {}).get("status_str")
    if status not in {"success", "error"}:
        return None
    outputs: list[str] = []
    for output in entry.get("outputs", {}).values():
        for item in output.get("images", []):
            # ComfyUI exposes loaded input videos as images too. Only record
            # saved output artifacts when the node supplies an explicit type;
            # this keeps an assembly task from mistaking S01 input for its
            # resulting `assembly_*.mp4`.
            if item.get("type") not in {None, "output"}:
                continue
            if str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                subfolder = str(item.get("subfolder", "")).strip("/\\")
                outputs.append(f"{subfolder}/{item['filename']}" if subfolder else item["filename"])
    return {"status": status, "outputs": outputs, "raw_status": entry.get("status", {}),
            "comfy_timing": h3_duration.history_timing(entry)}


def attach_h3_duration_result(done: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get('h3_duration')
    if not isinstance(profile, dict):
        return done
    observed = None
    if done.get('status') == 'success' and done.get('outputs'):
        output = (OUTPUT_ROOT / str(done['outputs'][0])).resolve()
        if output.is_relative_to(OUTPUT_ROOT.resolve()) and output.is_file():
            observed = h3_duration.probe_video(output)
    timing = done.get('comfy_timing') if isinstance(done.get('comfy_timing'), dict) else {}
    execution = timing.get('comfy_execution_seconds')
    observed_seconds = observed.get('duration_seconds') if isinstance(observed, dict) else None
    done['h3_duration'] = {
        'plan': profile, 'observed_video': observed, 'timing': timing,
        'comfy_seconds_per_observed_output_second': round(execution / observed_seconds, 3)
        if isinstance(execution, (int, float)) and isinstance(observed_seconds, (int, float)) and observed_seconds > 0 else None,
    }
    return done


SEGMENT_GENERATION_FIELDS = (
    "duration_seconds", "location", "shot_size", "camera", "wardrobe",
    "performance", "prompt", "workflow", "keyframes", "audio", "reference_asset_id", "generation", "resolved_generation",
)


def segment_generation_snapshot(segment: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the editable inputs alongside the exact submitted execution inputs."""
    snapshot = {key: segment[key] for key in SEGMENT_GENERATION_FIELDS if key in segment}
    payload = payload or {}
    execution = payload.get("execution_snapshot")
    if isinstance(execution, dict):
        snapshot["execution_snapshot"] = execution
        if (payload.get('h3_duration') or {}).get('generation_mode') == 'text_to_video':
            snapshot['keyframes'] = {'first': None, 'last': None}
            snapshot['audio'] = {**(snapshot.get('audio') or {}), 'guide': None}
        if execution.get("prompt") is not None:
            snapshot["prompt"] = execution["prompt"]
        materials = execution.get("materials") or {}
        if isinstance(materials, dict):
            if "first-frame" in materials or "last-frame" in materials:
                frames = dict(snapshot.get("keyframes") or {})
                if "first-frame" in materials:
                    frames["first"] = materials["first-frame"]
                if "last-frame" in materials:
                    frames["last"] = materials["last-frame"]
                snapshot["keyframes"] = frames
            if "guide" in materials:
                audio = dict(snapshot.get("audio") or {})
                audio["guide"] = materials["guide"]
                snapshot["audio"] = audio
    if payload.get("pipeline_values"):
        snapshot["pipeline_values"] = payload["pipeline_values"]
    if payload.get("seed") is not None:
        snapshot["seed"] = payload["seed"]
    return json.loads(json.dumps(snapshot, ensure_ascii=False))


def system_time_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@plan_transaction
def record_segment_result(
    asset_id: str, prompt_id: str, workflow: str, done: dict[str, Any],
    plan_path: Path | None = None, *, task_id: str | None = None,
    payload: dict[str, Any] | None = None, generated_at: float | None = None,
) -> None:
    """Make a successful generation current while retaining older versions."""
    target_plan = plan_path or PLAN_PATH
    if not target_plan.is_file() or not done.get("outputs"):
        return
    document = json.loads(target_plan.read_text(encoding="utf-8-sig"))
    segments = document.get("segments")
    if not isinstance(segments, list):
        return
    segment = next((item for item in segments if isinstance(item, dict) and item.get("id") == asset_id), None)
    if segment is None:
        return
    output = str(done["outputs"][0]).replace("\\", "/").lstrip("/")
    video_path = (done.get('published_outputs') or [f"output/{output}"])[0]
    version_task_id = task_id or prompt_id
    timestamp = generated_at if generated_at is not None else time.time()
    snapshot = (payload or {}).get("segment_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = segment_generation_snapshot(segment, payload)
    else:
        snapshot = json.loads(json.dumps(snapshot, ensure_ascii=False))
        execution = (payload or {}).get("execution_snapshot")
        if isinstance(execution, dict):
            snapshot["execution_snapshot"] = execution
            if execution.get("prompt") is not None:
                snapshot["prompt"] = execution["prompt"]
            materials = execution.get("materials") or {}
            if isinstance(materials, dict):
                if "first-frame" in materials or "last-frame" in materials:
                    frames = dict(snapshot.get("keyframes") or {})
                    if "first-frame" in materials:
                        frames["first"] = materials["first-frame"]
                    if "last-frame" in materials:
                        frames["last"] = materials["last-frame"]
                    snapshot["keyframes"] = frames
                if "guide" in materials:
                    audio = dict(snapshot.get("audio") or {})
                    audio["guide"] = materials["guide"]
                    snapshot["audio"] = audio
    history = segment.setdefault("generation_history", [])
    if not any(isinstance(item, dict) and item.get("task_id") == version_task_id for item in history):
        history.append({
            "task_id": version_task_id, "prompt_id": prompt_id,
            "generated_at": timestamp, "video_path": video_path,
            "snapshot": snapshot, "workflow": workflow,
        })
    segment["status"] = "video_generated"
    # `workflow` is the executed, frozen API graph (kept in history above).
    # Keep the author's current recipe, including edits made while this task
    # ran. A snapshot path is not an installed pipeline recipe identity.
    segment["comfyui_task_id"] = prompt_id
    segment["current_version_task_id"] = version_task_id
    segment.pop("current_version_restored_at", None)
    segment["video"] = {"path": video_path, "variant": "v3-A"}
    segment["notes"] = "生成完成；当前展示最新版本，旧版本保留在历史记录。"
    assembly = document.get("assembly")
    if isinstance(assembly, dict) and assembly.get("output"):
        assembly.update(status="stale", notes="分镜已更新，需要重新装配当前版本。")
    write_plan_version(target_plan, document)


def write_recipe_snapshot(payload: dict[str, Any], *, project_id: str | None = None) -> str:
    """Persist the exact API recipe submitted to ComfyUI for this attempt.

    This is an execution snapshot, alongside the user-facing UI workflow. It is
    intentionally generated from the same adapter call used by h3_render.py.
    """
    module_spec = importlib.util.spec_from_file_location("director_h3_adapter", RENDERER)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"无法加载 ComfyUI 适配器: {RENDERER}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    graph = module.prepare_h3_graph(
        json.loads(TEMPLATE.read_text(encoding="utf-8")),
        prompt=payload["prompt"], first_frame=str(shared_path(payload["first_frame"])),
        last_frame=str(shared_path(payload["last_frame"])), duration_seconds=5,
        seed=payload["seed"], filename_prefix=payload["output_prefix"],
        aspect_ratio="9:16 (Portrait Widescreen)", megapixels=0.4, turbo=True,
        audio_guide=str(shared_path(payload["audio_guide"])), preserve_source_audio=False,
    )
    destination = task_snapshot_root(project_id or CURRENT_PROJECT_ID) / f"api-{uuid.uuid4().hex[:16]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(destination)


def advance_task_stop(task: dict[str, Any], prompt_id: str) -> bool:
    """Return true only when this monitoring loop can safely finish."""
    try:
        queue = request_json("/queue")
        if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ("queue_running", "queue_pending")):
            raise ValueError("ComfyUI 停止核对队列无效")
        def contains(items: list) -> bool:
            return any(isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id for item in items)
        if contains(queue['queue_running']):
            if task['status'] != 'stopping':
                request_json('/interrupt', {'prompt_id': prompt_id})
            set_task(task['id'], status='stopping')
            return False
        if contains(queue['queue_pending']):
            request_json('/queue', {'delete': [prompt_id]})
            set_task(task['id'], status='stopping')
            return False
        done = history_done(prompt_id)
        if done and done.get('status') == 'error':
            set_task(task['id'], status='stopped', result=json.dumps(done, ensure_ascii=False), error='ComfyUI 已确认任务中断或失败')
        elif done:
            reconcile_task(task)
        else:
            set_task(task['id'], status='needs_reconcile', error='停止后任务已离开队列，但没有终态回执；请核对原任务')
        return True
    except Exception as exc:
        set_task(task['id'], status='needs_reconcile', error=f'停止结果待核对: {exc}')
        return True


def update_comfy_execution_state(task_id: str, prompt_id: str) -> None:
    """Only positive queue evidence advances a task; absence is not completion."""
    try:
        queue = request_json('/queue')
    except (OSError, ValueError, HTTPError, URLError):
        return
    if not isinstance(queue, dict) or not isinstance(queue.get('queue_running'), list):
        return
    if any(isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id
           for item in queue['queue_running']):
        # A stop request may race this poll. Never overwrite it or a terminal state.
        with DB_LOCK, db() as connection:
            connection.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=? AND prompt_id=? AND status='queued' AND stop_requested=0",
                               (time.time(), task_id, prompt_id))
            connection.commit()


def comfy_execution_error(payload: dict[str, Any], done: dict[str, Any]) -> str:
    label = ('关键帧生成' if payload.get('kind') == 'keyframe' else
             '成片装配' if payload.get('assembly_asset_id') and payload.get('asset_id') == payload['assembly_asset_id'] else
             '视频生成')
    prefix = f'ComfyUI {label}失败'
    raw = done.get('raw_status')
    messages = raw.get('messages', []) if isinstance(raw, dict) else []
    if not isinstance(messages, list):
        return prefix
    for message in reversed(messages):
        if not isinstance(message, (list, tuple)) or len(message) != 2 or message[0] != 'execution_error' or not isinstance(message[1], dict):
            continue
        details = message[1]
        parts = [' '.join(str(details[key]).split())[:500] for key in ('node_type', 'exception_type', 'exception_message') if details.get(key)]
        if parts:
            return prefix + '：' + ' · '.join(parts)[:1000]
    return prefix


def run_submission(task_id: str, payload: dict[str, Any]) -> None:
    submission_attempted = False
    prompt_id = None
    try:
        set_task(task_id, status="submitting")
        first = shared_path(payload["first_frame"])
        last = shared_path(payload["last_frame"])
        guide = shared_path(payload["audio_guide"])
        required = [RENDERER, TEMPLATE, first, last, guide]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("缺少 ComfyUI 输入或工作流: " + "; ".join(missing))
        command = [sys.executable, str(RENDERER), "submit", "--api-url", COMFY_URL,
                   "--template", str(TEMPLATE), "--prompt", payload["prompt"],
                   "--first-frame", str(first), "--last-frame", str(last),
                   "--audio-guide", str(guide), "--duration", "5", "--seed", str(payload["seed"]),
                   "--aspect-ratio", "9:16 (Portrait Widescreen)", "--megapixels", "0.4",
                   "--output-prefix", payload["output_prefix"]]
        recipe_path = str((payload.get("execution_snapshot") or {}).get("api_graph") or write_recipe_snapshot(payload, project_id=str(payload.get("project_id") or CURRENT_PROJECT_ID)))
        submission_attempted = True
        result = subprocess.run(command, capture_output=True, text=True, cwd=RENDERER.parent, timeout=120, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"h3_render exited {result.returncode}")
        submitted = None
        decoder = json.JSONDecoder()
        # h3_render prints a human-readable JSON object; tolerate informational
        # lines before it without treating the final closing brace as JSON.
        for offset, character in enumerate(result.stdout):
            if character != "{":
                continue
            try:
                candidate, end = decoder.raw_decode(result.stdout[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("prompt_id"):
                submitted = candidate
                break
        if submitted is None:
            raise RuntimeError(f"无法解析 h3_render 提交回执: {result.stdout[-1200:]}")
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {submitted}")
        set_task(task_id, status="queued", prompt_id=prompt_id, result=json.dumps({"submission": submitted, "recipe": recipe_path}, ensure_ascii=False))
        while True:
            current = get_task(task_id)
            if not current:
                return
            if current["stop_requested"]:
                if advance_task_stop(current, str(prompt_id)):
                    return
                time.sleep(2)
                continue
            sample_resources(task_id)
            done = history_done(prompt_id)
            if done:
                if done["status"] == "success":
                    current = get_task(task_id)
                    if current:
                        done = publish_task_outputs(current, done)
                    target_plan = Path(str(payload.get("plan_path"))) if payload.get("plan_path") else PLAN_PATH
                    record_segment_result(
                        str(payload.get("asset_id", "")), str(prompt_id), str(recipe_path), done,
                        target_plan, task_id=task_id, payload=payload,
                    )
                    set_task(task_id, status="succeeded", result=json.dumps(done, ensure_ascii=False))
                else:
                    set_task(task_id, status="failed", result=json.dumps(done, ensure_ascii=False), error=comfy_execution_error(payload, done))
                return
            update_comfy_execution_state(task_id, str(prompt_id))
            time.sleep(2)
    except Exception as exc:
        set_task(task_id, status="needs_reconcile" if submission_attempted else "failed", error=("提交或监控结果待核对: " if submission_attempted else "") + str(exc))


def assembly_duration(output: str) -> float | None:
    import av
    try:
        path = (OUTPUT_ROOT / output).resolve()
        if not path.is_relative_to(OUTPUT_ROOT.resolve()):
            return None
        with av.open(str(path)) as media:
            if not media.streams.video or media.duration is None:
                return None
            duration = float(media.duration / av.time_base)
            return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, av.error.FFmpegError):
        return None


@plan_transaction
def record_assembly_result(prompt_id: str, workflow: str, done: dict[str, Any], plan_path: Path | None = None) -> None:
    """Persist a successful ComfyUI assembly as a reviewable candidate."""
    target_plan = plan_path or PLAN_PATH
    if not target_plan.is_file() or not done.get("outputs"):
        return
    document = json.loads(target_plan.read_text(encoding="utf-8-sig"))
    previous = document.get("assembly") if isinstance(document.get("assembly"), dict) else {}
    output = str(done["outputs"][0]).replace("\\", "/").lstrip("/")
    candidate_output = (done.get('published_outputs') or [f"output/{output}"])[0]
    if previous.get("output") != candidate_output or previous.get("prompt_id") != prompt_id:
        # Resolve ownership from the frozen target plan, never the selected UI
        # project: a completion may arrive after the user switches projects.
        for entry in load_project_catalog()["projects"]:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            try:
                owner = load_project_manifest(str(entry["id"]))
                owner_plan = resolve_registered_path(owner["plan_path"])
            except (RuntimeError, KeyError, TypeError, ValueError, HTTPException):
                # An unrelated broken catalog entry must not prevent this
                # project's completed candidate from being recorded.
                continue
            if owner_plan != target_plan.resolve():
                continue
            asset_id = str(owner.get("assembly_asset_id", "MASTER"))
            with DB_LOCK, db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT data FROM project_records WHERE project_id=? AND asset_id=? AND record_type='review:final' ORDER BY revision DESC, id DESC LIMIT 1",
                    (owner["id"], asset_id),
                ).fetchone()
                if row:
                    review = json.loads(row["data"])
                    if review.get("status") != "stale":
                        append_project_record_in_connection(
                            connection, owner["id"], "review:final",
                            {**review, "status": "stale", "note": "成片候选已变化，需要重新审核"},
                            asset_id, "assembly-result", time.time(),
                        )
                connection.commit()
            break
    document["assembly"] = {
        **previous,
        "status": "candidate_generated",
        "prompt_id": prompt_id,
        "workflow": workflow,
        "output": candidate_output,
        "duration_seconds": assembly_duration(output),
        "notes": "ComfyUI history 返回成功；候选成片已由导演台重新装配，仍待人工审核，不视为已采用。",
    }
    write_plan_version(target_plan, document)


def bind_approved_assembly_candidates(document: dict[str, Any], state: dict[str, Any], finish_required: bool) -> None:
    """Bind reviewed candidates in the in-memory snapshot, preserving the plan."""
    stage = "finish" if finish_required else "sample"
    for segment in document.get("segments", []):
        review = state.get("reviews", {}).get(str(segment.get("id")), {}).get(stage, {})
        reference = review.get("candidate_ref")
        if review.get("status") != "approved" or not reference:
            # Legacy approvals did not record a candidate; keep their plan input.
            continue
        reference = str(reference)
        if reference.startswith("/media-file?"):
            values = parse_qs(urlsplit(reference).query).get("path", [])
            if len(values) != 1:
                raise ValueError("审核候选链接缺少唯一素材路径")
            reference = values[0]
        path = resolve_registered_path(reference)
        if not path.is_file():
            raise ValueError(f"{segment.get('id')} 已采用的视频不存在，请重新审核候选")
        segment["video"] = {**segment.get("video", {}), "path": str(path)}


def assembly_builder_path(manifest: dict[str, Any] | None = None) -> Path:
    if manifest is None:
        return ASSEMBLY_BUILDER
    return PROJECT / str((manifest.get('assembly') or {}).get('builder', 'tools/build_assembly_workflow.py'))


def assembly_builder_blocker(manifest: dict[str, Any] | None = None):
    if not assembly_builder_path(manifest).is_file():
        project_id = str(manifest['id']) if manifest is not None else CURRENT_PROJECT_ID
        return {'code': 'assembly_builder_missing', 'message': '装配能力尚未配置完整，请联系管理员检查构建器',
                'action': {'id': 'inspect_assembly_config', 'label': '查看制作方案并联系管理员',
                           'method': 'GET', 'path': f'/api/projects/{project_id}/pipeline'}}
    return None


def build_assembly_snapshot(manifest: dict[str, Any] | None = None) -> Path:
    """Materialize an assembly graph from the current plan before submission.

    Segment regeneration updates the plan's video path. Reusing the checked-in
    graph would silently assemble an older input, so every run receives an
    immutable, reviewable snapshot generated from the latest plan.
    """
    project = manifest if manifest is not None else CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else CURRENT_PROJECT_ID
    plan_path = resolve_registered_path(str(project['plan_path'])) if manifest is not None else PLAN_PATH
    config = dict(project.get('assembly') or {}) if manifest is not None else ASSEMBLY_CONFIG
    builder = assembly_builder_path(manifest)
    if not plan_path.is_file():
        raise FileNotFoundError(f"缺少分段计划: {plan_path}")
    if not builder.is_file():
        raise FileNotFoundError(f"缺少装配工作流构建器: {builder}")
    module_spec = importlib.util.spec_from_file_location("director_assembly_builder", builder)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"无法加载装配工作流构建器: {builder}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    blocking = assembly_edit.blocking_joins(document)
    if blocking:
        raise ValueError(f"接点尚未按当前视频版本审核通过: {', '.join(blocking)}")
    stages = project.get('pipeline', []) if manifest is not None else PIPELINE_STAGES
    # The plan's current video is the source of truth. Legacy review records
    # must not silently replace it with an older approved candidate.
    video_blockers = assembly_video_blockers(document)
    if video_blockers:
        raise ValueError('; '.join(item['message'] for item in video_blockers))
    snapshot_dir = task_snapshot_root(project_id)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    project_label = str(project.get("title", project_id)).replace("/", "_")
    snapshot = snapshot_dir / f"assembly-{uuid.uuid4().hex[:12]}.json"
    # A saved project selection overrides legacy manifest audio, including an
    # explicit choice to keep each segment's native audio.
    sound = assembly_sound_selection(project, document)
    audio_reference = str(sound['path'] or '')
    filename_prefix = str(config.get("filename_prefix", f"导演台/{project_id}/assembly"))
    graph = module.build_graph(document, audio_reference, filename_prefix)
    snapshot.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def comfy_submission_error(exc: HTTPError) -> str:
    try:
        response = json.loads(exc.read().decode("utf-8", errors="replace"))
        messages = []
        for node_id, node in (response.get("node_errors") or {}).items():
            for error in node.get("errors", []):
                messages.append(f"{node.get('class_type', node_id)}：{error.get('details') or error.get('message') or '输入无效'}")
        if messages:
            return "ComfyUI 输入校验失败：" + "；".join(messages)[:1600]
        error = response.get("error", {})
        return f"ComfyUI 拒绝提交（{exc.code}）：{error.get('message', str(exc))}" if isinstance(error, dict) else f"ComfyUI 拒绝提交（{exc.code}）：{error}"
    except (ValueError, OSError, AttributeError, TypeError):
        return f"ComfyUI 拒绝提交：{exc}"


def run_workflow_submission(task_id: str, payload: dict[str, Any]) -> None:
    """Submit a saved API graph to ComfyUI and track it like a segment task."""
    submission_attempted = False
    prompt_id = None
    submission_acknowledged = False
    try:
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        set_task(task_id, status="submitting")
        workflow_path = Path(payload["workflow"])
        if not workflow_path.is_file():
            raise FileNotFoundError(f"缺少 ComfyUI 工作流: {workflow_path}")
        graph = json.loads(workflow_path.read_text(encoding="utf-8"))
        # This ComfyUI installation accepts a client-provided UUID. Persist it
        # before sending so a lost HTTP response still has a lookup identity.
        prompt_id = task_id
        set_task(task_id, prompt_id=prompt_id)
        submission_attempted = True
        submitted = request_json("/prompt", {"prompt": graph, "prompt_id": prompt_id, "client_id": "director-workbench-assembly-v2"})
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {submitted}")
        submission_acknowledged = True
        set_task(task_id, status="queued", prompt_id=prompt_id, result=json.dumps({"submission": submitted, "workflow": str(workflow_path)}, ensure_ascii=False))
        while True:
            current = get_task(task_id)
            if not current:
                return
            if current["stop_requested"]:
                if advance_task_stop(current, str(prompt_id)):
                    return
                time.sleep(2)
                continue
            sample_resources(task_id)
            done = (keyframes.read_result(request_json(f'/history/{prompt_id}'), str(prompt_id), payload, OUTPUT_ROOT)
                     if payload.get('kind') == 'keyframe' else history_done(prompt_id))
            if done:
                done = attach_h3_duration_result(done, payload)
                if done["status"] == "success":
                    if payload.get('kind') == 'keyframe':
                        complete_keyframe_task(current, done)
                        return
                    done = publish_task_outputs(current, done)
                    target_plan = Path(str(payload.get("plan_path"))) if payload.get("plan_path") else PLAN_PATH
                    if payload.get("asset_id") == str(payload.get("assembly_asset_id", CURRENT_PROJECT.get("assembly_asset_id", "MASTER"))):
                        record_assembly_result(prompt_id, str(workflow_path), done, target_plan)
                    else:
                        record_segment_result(
                            str(payload.get("asset_id", "")), prompt_id, str(workflow_path), done,
                            target_plan, task_id=task_id, payload=payload,
                        )
                    set_task(task_id, status="succeeded", result=json.dumps(done, ensure_ascii=False))
                else:
                    set_task(task_id, status="failed", result=json.dumps(done, ensure_ascii=False), error=comfy_execution_error(payload, done))
                return
            update_comfy_execution_state(task_id, str(prompt_id))
            time.sleep(2)
    except HTTPError as exc:
        uncertain = submission_acknowledged or exc.code >= 500
        fields = {} if uncertain else {'prompt_id': None}
        set_task(task_id, status="needs_reconcile" if uncertain else "failed", error=comfy_submission_error(exc), **fields)
    except Exception as exc:
        set_task(task_id, status="needs_reconcile" if submission_attempted else "failed", error=("提交或监控结果待核对: " if submission_attempted else "") + str(exc))


def run_batch(batch_id: str, task_ids: list[str], payloads: list[dict[str, Any]]) -> None:
    for task_id, payload in zip(task_ids, payloads):
        with DB_LOCK, db() as connection:
            claimed = connection.execute("UPDATE tasks SET status='preparing',updated_at=? WHERE id=? AND batch_id=? AND status='batch_waiting' AND stop_requested=0", (time.time(), task_id, batch_id)).rowcount
            connection.commit()
        if not claimed:
            break
        runner = run_workflow_submission if payload.get("workflow") else run_submission
        runner(task_id, payload)
        current = get_task(task_id)
        if not current or current["status"] != "succeeded":
            # A failed or interrupted segment is a recovery point; do not
            # silently dispatch later segments.
            for remaining in task_ids[task_ids.index(task_id) + 1:]:
                if get_task(remaining):
                    set_task(remaining, status="stopped", error="批次在前段停止，未提交到 ComfyUI")
            break


class StartRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    asset_id: str = Field(default="")
    prompt: str | None = None
    seed: int | None = None
    pipeline_stage_id: str | None = Field(default=None, max_length=120)
    pipeline_values: dict[str, Any] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    asset_ids: list[str] = Field(default_factory=list, max_length=128)
    prompt: str | None = None
    pipeline_stage_id: str | None = Field(default=None, max_length=120)
    pipeline_values_by_asset: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PipelineValidateRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    asset_id: str | None = Field(default=None, max_length=160)


class ProjectSelectRequest(BaseModel):
    project_id: str


class ProjectImportRequest(BaseModel):
    path: str
    project_id: str | None = None


class ProjectCreateRequest(BaseModel):
    creation_mode: Literal['auto', 'advanced'] = 'auto'
    series: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    project_id: str | None = None
    select: bool = True


class SeriesCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AssetLabelRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    expected_title: str


class DirectoryDeleteRequest(BaseModel):
    plan_id: str
    confirm: str


class SegmentDeleteRequest(BaseModel):
    expected_revision: int
    confirm: str


class PipelineStageCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(default="", max_length=500)
    backend: str = Field(default="混合流程", max_length=40)
    inputs: list[str] = Field(default_factory=list, max_length=32)
    outputs: list[str] = Field(default_factory=list, max_length=32)
    workflow: str = Field(default="", max_length=500)


ShotGenerationRequest = shot_creation.GenerationSettings


class PlanSegmentCreateRequest(BaseModel):
    generation: ShotGenerationRequest | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, ge=0)
    segment_id: str | None = None
    duration_seconds: float = Field(gt=0, le=600)
    location: str = Field(default="待定场景", max_length=160)
    shot_size: str = Field(default="中景", max_length=80)
    camera: str = Field(default="固定机位", max_length=160)
    wardrobe: str = Field(default="沿用角色设定", max_length=160)
    performance: str = Field(default="", max_length=1000)
    prompt: str = Field(default="", max_length=100000)
    workflow: str | None = Field(default=None, max_length=500)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)


class CreativeSettingsRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    values: dict[str, Any] = Field(default_factory=dict)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    source: str = Field(default="director-ui", max_length=80)


class ProjectCreativeSettingsRequest(CreativeSettingsRequest):
    expected_revision: int = Field(ge=0)


class ReviewRecordRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    asset_id: str = Field(min_length=1, max_length=160)
    stage: Literal['sample', 'finish', 'final'] = 'sample'
    status: Literal['pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    note: str = Field(default="", max_length=4000)
    adopted_variant: str | None = Field(default=None, max_length=80)
    candidate_ref: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="director-ui", max_length=80)


class CheckpointRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    stage_id: str = Field(min_length=1, max_length=120)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    asset_id: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=4000)
    upstream_revisions: dict[str, int] = Field(default_factory=dict)
    source: str = Field(default="director-ui", max_length=80)


class ArtifactRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=240)
    content: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list, max_length=64)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    asset_id: str = Field(default="", max_length=160)
    source: str = Field(default="director-ui", max_length=80)


class ProjectReviewRecordRequest(ReviewRecordRequest):
    expected_revision: int = Field(ge=0)

class AdoptionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=160)
    stage: Literal['sample', 'finish', 'final']
    candidate_ref: str = Field(min_length=1, max_length=1000)
    expected_review_revision: StrictInt = Field(ge=0)
    expected_plan_revision: StrictInt = Field(ge=0)
    note: str = Field(min_length=1, max_length=4000)
    confirm_adoption: StrictBool = Field(json_schema_extra={'const': True})

    @field_validator('confirm_adoption')
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError('需要明确确认采用此候选')
        return value

class ProjectCheckpointRequest(CheckpointRequest):
    expected_revision: int = Field(ge=0)

class ProjectArtifactRequest(ArtifactRequest):
    expected_revision: int = Field(ge=0)


class ReverseArtifactUpdateRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    summary: str = Field(default="", max_length=8000)
    user_status: str = Field(default="reviewed", max_length=40)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReverseConvertRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class ReverseClassificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    content_mode: Literal['narrative', 'music_performance', 'dance', 'showcase', 'mood', 'technical', 'other']
    reason: str = Field(min_length=1, max_length=2000)


class ReverseTimelineUpdateRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    user_note: str = Field(default="", max_length=4000)
    status: str = Field(default="reviewed", max_length=40)


class MaterialRegisterItem(BaseModel):
    source_id: str = Field(min_length=1, max_length=40)
    relative_path: str = Field(min_length=1, max_length=1000)


class MaterialRegisterRequest(BaseModel):
    items: list[MaterialRegisterItem] = Field(min_length=1, max_length=128)


class MaterialReuseRequest(BaseModel):
    source_project_id: str = Field(min_length=1, max_length=120)
    material_ids: list[str] = Field(min_length=1, max_length=128)


class PlanSegmentUpdateRequest(BaseModel):
    generation: ShotGenerationRequest | None = None
    reference_asset_id: str | None = Field(default=None, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    location: str | None = Field(default=None, max_length=160)
    shot_size: str | None = Field(default=None, max_length=80)
    camera: str | None = Field(default=None, max_length=160)
    wardrobe: str | None = Field(default=None, max_length=160)
    performance: str | None = Field(default=None, max_length=1000)
    prompt: str | None = Field(default=None, max_length=100000)
    workflow: str | None = Field(default=None, max_length=500)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)
    dependencies: list[str] | None = Field(default=None, max_length=64)


class SegmentVersionRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_revision: StrictInt = Field(ge=0)


class RecipeIdentityRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=160)
    expected_revision: StrictInt = Field(ge=0)
    dry_run: bool = True


class PlanSegmentOperationRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    operation: str = Field(max_length=40)
    target_id: str | None = Field(default=None, max_length=160)
    split_at_seconds: float | None = Field(default=None, gt=0, le=600)
    before_id: str | None = Field(default=None, max_length=160)


@app.get("/api/project")
def current_project() -> dict[str, Any]:
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        project_id = PRIVATE_WORKSPACES.selected()
        if not project_id:
            raise HTTPException(404, '请创建或选择作品')
        return public_project(require_project_manifest(project_id))
    return public_project()


def build_project_state(project_id: str | None = None) -> dict[str, Any]:
    target_project = project_id or CURRENT_PROJECT_ID
    records = project_records(target_project)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        latest[(record["record_type"], record["asset_id"])] = record
    creative_record = latest.get(("creative_settings", ""))
    reviews: dict[str, dict[str, Any]] = {}
    checkpoints: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []
    for (record_type, asset_id), record in latest.items():
        data = {**record["data"], "revision": record["revision"], "source": record["source"], "created_at": record["created_at_iso"]}
        if record_type.startswith("review:"):
            reviews.setdefault(asset_id, {})[record_type.split(":", 1)[1]] = data
        elif record_type.startswith("checkpoint:"):
            stage_id = record_type.split(":", 1)[1]
            checkpoints[f"{asset_id}:{stage_id}" if asset_id else stage_id] = data
        elif record_type.startswith("artifact:"):
            artifacts.append({"kind": record_type.split(":", 1)[1], "asset_id": asset_id, **data})
    return {
        "schema_version": 1,
        "project_id": target_project,
        "creative": ({**creative_record["data"], "revision": creative_record["revision"], "source": creative_record["source"], "created_at": creative_record["created_at_iso"]} if creative_record else None),
        "reviews": reviews,
        "checkpoints": checkpoints,
        "artifacts": sorted(artifacts, key=lambda item: (item.get("kind", ""), item.get("asset_id", ""))),
        "history": records,
    }


@app.get("/api/project-state")
def get_project_state() -> dict[str, Any]:
    state = build_project_state()
    state['readiness'] = project_readiness(CURRENT_PROJECT_ID, state)
    return state


class GuideSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    status: Literal['active', 'skipped'] | None = None
    project_id: str | None = Field(default=None, max_length=160)
    route: Literal['idea', 'reference'] | None = None
    completed_steps: list[str] | None = Field(default=None, max_length=12)


def settings_owner() -> str:
    principal = current_principal.get()
    return principal.identity.username if principal else '__legacy__'


def guide_settings_value(connection, owner):
    row = connection.execute('SELECT revision,guide FROM user_settings WHERE owner=?', (owner,)).fetchone()
    return ({**json.loads(row['guide']), 'revision': row['revision']} if row else
            {'status': 'active', 'project_id': None, 'route': None, 'completed_steps': [], 'revision': 0})


@app.get('/api/settings/guide')
def get_guide_settings(project_id: str | None = None) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        value = guide_settings_value(connection, settings_owner())
    readiness = None
    manifest = None
    selected_id = project_id or value['project_id']
    if selected_id:
        try:
            manifest = require_project_manifest(selected_id)
            readiness = project_readiness(selected_id, build_project_state(selected_id))
        except HTTPException:
            if project_id:
                raise
            value['project_id'] = None
    return {**value, 'steps': guide.steps(readiness, has_project=manifest is not None), 'readiness': readiness, 'view_project_id': selected_id if manifest else None}


@app.patch('/api/settings/guide')
def update_guide_settings(request: GuideSettingsRequest) -> dict[str, Any]:
    if request.project_id:
        require_project_manifest(request.project_id)
    known = {step[0] for step in guide.STEPS}
    if request.completed_steps is not None and (len(request.completed_steps) != len(set(request.completed_steps)) or set(request.completed_steps) - known):
        raise HTTPException(422, {'code': 'invalid_guide_step', 'message': '请选择指南中的步骤'})
    with DB_LOCK, db() as connection:
        owner = settings_owner()
        value = guide_settings_value(connection, owner)
        if request.expected_revision != value['revision']:
            raise HTTPException(409, {'code': 'settings_revision_conflict', 'message': '指南进度已变化，请重新读取', 'current_revision': value['revision']})
        changes = request.model_dump(exclude={'expected_revision'}, exclude_unset=True)
        if any(changes.get(key, 'valid') is None for key in ('status', 'completed_steps')):
            raise HTTPException(422, {'code': 'invalid_guide_settings', 'message': '指南状态和步骤不能设为空'})
        value.update(changes)
        revision = value.pop('revision') + 1
        connection.execute('INSERT INTO user_settings(owner,revision,guide) VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision,guide=excluded.guide',
                           (owner, revision, json.dumps(value, ensure_ascii=False)))
        connection.commit()
    return get_guide_settings()


@app.get('/api/projects/{project_id}/creative-template')
def project_creative_template(project_id: str) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    creative = build_project_state(project_id)['creative']
    if not creative or not isinstance(creative.get('values'), dict):
        raise HTTPException(409, '参考作品尚未保存创作设定')
    source = creative['values']
    provenance = {'project_id': project_id, 'series': manifest.get('series', ''),
                  'title': manifest.get('title', project_id), 'revision': creative['revision'],
                  'status': creative.get('status', 'pending_review')}
    values = {name: str(source.get(name) or '') for name in ('world', 'character', 'voice')}
    values.update(creation_mode='reference-series', intent='', adaptation='', notes='', reference_video='',
                  reference_series='\n'.join(str(value) for value in (
                      provenance['series'], provenance['title'], f"来源 {project_id} · 版本 {creative['revision']}",
                      source.get('reference_series')) if value))
    return {'source': provenance, 'values': values, 'status': 'pending_review'}


@plan_transaction
def confirm_creative_settings_atomically(request: CreativeSettingsRequest, project_id: str | None = None) -> dict[str, Any]:
    """Persist settings and artifacts atomically, including approval when requested."""
    target_project = project_id or CURRENT_PROJECT_ID
    try:
        document, _ = load_project_plan(target_project)
    except HTTPException:
        document = {"segments": []}
    now = time.time()
    artifact_specs = {
        "intent": ("intent", "world", "notes", "reference_video", "reference_series", "adaptation"),
        "character": ("character",),
        "voice": ("voice",),
    }
    checkpoint_rows: list[sqlite3.Row] = []
    with DB_LOCK, db() as connection:
        previous_row = connection.execute(
            "SELECT data, revision FROM project_records WHERE project_id = ? AND asset_id = '' AND record_type = 'creative_settings' ORDER BY revision DESC LIMIT 1",
            (target_project,),
        ).fetchone()
        current_revision = previous_row['revision'] if previous_row else 0
        if request.expected_revision is not None and request.expected_revision != current_revision:
            raise HTTPException(409, {'code': 'creative_revision_conflict', 'message': '创作设定已被修改，请读取最新版本后合并草稿', 'current_revision': current_revision, 'expected_revision': request.expected_revision, 'retryable': False})
        previous_values = json.loads(previous_row["data"]).get("values", {}) if previous_row else {}
        changed_upstream = bool(previous_row or request.status == 'approved') and any((previous_values.get(key) or "") != (request.values.get(key) or "") for key in ("intent", "world", "character", "voice", "reference_video", "reference_series", "adaptation"))
        creative_row = append_project_record_in_connection(
            connection, target_project, "creative_settings", {"values": request.values, "status": request.status}, source=request.source, created_at=now
        )
        for kind, keys in artifact_specs.items():
            content = {key: request.values.get(key) for key in keys if request.values.get(key) not in (None, "")}
            if content:
                append_project_record_in_connection(
                    connection, target_project, f"artifact:{kind}", {"title": kind, "content": content, "references": [], "status": request.status}, source=request.source, created_at=now
                )
        for stage_id in (("intent", "character") if request.status == 'approved' else ()):
            checkpoint_rows.append(
                append_project_record_in_connection(
                    connection,
                    target_project,
                    f"checkpoint:{stage_id}",
                    {"stage_id": stage_id, "status": "approved", "note": f"创作设定 revision {creative_row['revision']}", "upstream_revisions": {}},
                    source=request.source,
                    created_at=now,
                )
            )
        if changed_upstream:
            latest_reviews: dict[tuple[str, str], dict[str, Any]] = {}
            review_rows = connection.execute(
                "SELECT asset_id, record_type, data FROM project_records WHERE project_id = ? AND record_type IN ('review:sample', 'review:finish') ORDER BY revision DESC, id DESC",
                (target_project,),
            ).fetchall()
            for row in review_rows:
                latest_reviews.setdefault((str(row["asset_id"]), str(row["record_type"])), json.loads(row["data"]))
            for segment in document.get("segments", []):
                if not isinstance(segment, dict) or not segment.get("id"):
                    continue
                segment_id = str(segment["id"])
                for stage_id in ("scene", "performance", "storyboard", "materials", "generate", "review"):
                    append_project_record_in_connection(
                        connection,
                        target_project,
                        f"checkpoint:{stage_id}",
                        {"stage_id": stage_id, "status": "stale", "note": "创作设定上游版本变化，需要重新确认", "upstream_revisions": {}},
                        asset_id=segment_id,
                        source="upstream-change",
                        created_at=now,
                    )
                for review_stage in ("sample", "finish"):
                    existing = latest_reviews.get((segment_id, f"review:{review_stage}"))
                    if existing:
                        append_project_record_in_connection(
                            connection,
                            target_project,
                            f"review:{review_stage}",
                            {"stage": review_stage, "status": "stale", "note": "创作设定上游版本变化，需要重新确认", "adopted_variant": existing.get("adopted_variant"), "candidate_ref": existing.get("candidate_ref")},
                            asset_id=segment_id,
                            source="upstream-change",
                            created_at=now,
                        )
        connection.commit()
    creative = record_row(creative_row)
    return {
        **creative["data"],
        "revision": creative["revision"],
        "source": creative["source"],
        "created_at": creative["created_at_iso"],
        "checkpoints": [record_row(row) for row in checkpoint_rows],
        "atomic": True,
    }


@app.post("/api/project-state/creative/confirm")
def confirm_creative_settings(request: CreativeSettingsRequest) -> dict[str, Any]:
    if request.status != "approved":
        raise HTTPException(422, "原子确认接口只接受 approved 状态")
    return confirm_creative_settings_atomically(request)


@app.post("/api/project-state/creative")
def save_creative_settings(request: CreativeSettingsRequest) -> dict[str, Any]:
    if request.status not in {"draft", "pending_review", "approved", "changes_requested", "stale"}:
        raise HTTPException(422, "不支持的设定状态")
    return confirm_creative_settings_atomically(request)


@app.post("/api/projects/{project_id}/creative")
def save_project_creative_settings(project_id: str, request: ProjectCreativeSettingsRequest) -> dict[str, Any]:
    require_project_manifest(project_id)
    if request.status not in {"draft", "pending_review", "approved", "changes_requested", "stale"}:
        raise HTTPException(422, "不支持的设定状态")
    return confirm_creative_settings_atomically(request, project_id)


@app.post("/api/project-state/reviews")
def save_review(request: ReviewRecordRequest) -> dict[str, Any]:
    return persist_review(request)


@app.post("/api/projects/{project_id}/reviews")
def save_project_review(project_id: str, request: ProjectReviewRecordRequest) -> dict[str, Any]:
    require_project_manifest(project_id)
    return persist_review(request, project_id)


@plan_transaction
def persist_review(request: ReviewRecordRequest, project_id: str | None = None) -> dict[str, Any]:
    if request.stage not in {"sample", "finish", "final"}:
        raise HTTPException(422, "审核阶段必须是 sample、finish 或 final")
    if request.status not in {"pending_review", "approved", "changes_requested", "stale"}:
        raise HTTPException(422, "不支持的审核状态")
    if request.stage == "final" and request.status == "approved":
        owner_id = project_id or CURRENT_PROJECT_ID
        manifest = load_project_manifest(owner_id)
        if request.asset_id == str(manifest.get("assembly_asset_id") or "MASTER"):
            document, _ = load_project_plan(owner_id)
            if (document.get("assembly") or {}).get("status") == "stale":
                raise HTTPException(409, "制作计划已变化，请重新装配后再采用成片；旧成片仍可查看和记录意见")
    data = {
        "stage": request.stage,
        "status": request.status,
        "note": request.note,
        "adopted_variant": request.adopted_variant,
        "candidate_ref": request.candidate_ref,
    }
    record = append_project_record(f"review:{request.stage}", data, asset_id=request.asset_id, source=request.source, project_id=project_id, expected_revision=request.expected_revision)
    return {**data, "asset_id": request.asset_id, "revision": record["revision"], "source": record["source"], "created_at": record["created_at_iso"]}


def adoption_candidate_path(reference: str) -> Path:
    if reference.startswith('/media-file?'):
        parsed = urlsplit(reference)
        query = parse_qs(parsed.query)
        if parsed.fragment or set(query) != {'path'} or len(query['path']) != 1:
            raise HTTPException(422, '候选链接必须包含唯一素材路径')
        reference = query['path'][0]
    try:
        return resolve_registered_path(reference)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, '候选路径无效或不可访问') from exc


@app.post('/api/projects/{project_id}/adoptions')
@plan_transaction
def adopt_project_candidate(project_id: str, request: AdoptionRequest) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, _ = load_project_plan(project_id)
    check_plan_revision(document, request.expected_plan_revision)
    task_id = None
    registered = None
    material_kind_expected = None
    adopted_variant = 'A'
    candidate = adoption_candidate_path(request.candidate_ref)
    if request.asset_id == str(manifest.get('assembly_asset_id') or 'MASTER'):
        assembly = document.get('assembly') or {}
        blocking = assembly_edit.blocking_joins(document)
        if blocking:
            raise HTTPException(409, f"接点审核未通过或已过期: {', '.join(blocking)}")
        if request.stage != 'final' or assembly.get('status') != 'candidate_generated':
            raise HTTPException(409, '当前成片候选不可采用，请先完成当前计划的装配')
        task_id, registered = assembly.get('prompt_id'), assembly.get('output')
    else:
        segment = next((item for item in document.get('segments', []) if item.get('id') == request.asset_id), None)
        if segment is not None:
            if request.stage == 'final' or segment.get('status') != 'video_generated':
                raise HTTPException(409, '当前分镜没有可采用的生成候选')
            video = segment.get('video') or {}
            task_id, registered = segment.get('comfyui_task_id'), video.get('path')
            alternate = video.get('rework_candidate') or {}
            if not alternate.get('path'):
                alternate = video.get('finish_review') or {}
            if alternate.get('path') and candidate == adoption_candidate_path(str(alternate['path'])):
                task_id, registered = alternate.get('prompt_id'), alternate['path']
                adopted_variant = 'B'
        else:
            asset = next((item for item in manifest.get('assets', []) if item.get('id') == request.asset_id), None)
            if not asset or request.stage != 'sample' or not asset.get('ready'):
                raise HTTPException(409, '此素材或阶段没有可采用的当前候选')
            for field, kind in (('keyframe_task_id', 'keyframe'), ('speech_task_id', 'speech'), ('audio_edit_task_id', 'audio_edit'), ('media_task_id', 'media_operation')):
                if asset.get(field):
                    task_id, material_kind_expected = asset[field], kind
                    break
            registered = asset.get('image_path') or (asset.get('sources') or {}).get('A')
    if not task_id or not registered:
        raise HTTPException(409, '当前候选缺少生成任务关联，不能仅凭文件采用')
    task = get_task(str(task_id))
    media_output_matches = bool(task and material_kind_expected == 'media_operation' and any(
        item.get('asset_id') == request.asset_id and item.get('kind') in {'audio', 'video', 'image'}
        and Path(str(item.get('path', ''))).resolve() == candidate
        for item in (task.get('result') or {}).get('outputs', []) if isinstance(item, dict)))
    if not task or task.get('project_id') != project_id or (task.get('asset_id') != request.asset_id and not media_output_matches) or task.get('status') != 'succeeded':
        raise HTTPException(409, '候选未关联本作品此素材的成功任务')
    payload = task.get('payload') or {}
    if request.stage == 'final':
        if payload.get('asset_id') != request.asset_id or payload.get('assembly_asset_id') != request.asset_id or not payload.get('source_plan') or not payload.get('workflow') or payload.get('kind') not in (None, 'assembly') or payload.get('pipeline_stage_id'):
            raise HTTPException(409, '成片候选未关联真实装配任务')
    elif material_kind_expected:
        if payload.get('kind') != material_kind_expected:
            raise HTTPException(409, '素材与生成任务类型不一致')
    elif request.stage != 'final':
        if payload.get('kind') not in (None, 'workflow') or payload.get('source_plan'):
            raise HTTPException(409, '分镜候选未关联视频生成任务')
        task_stage = 'finish' if payload.get('pipeline_stage_id') == 'picture-finish' else 'sample'
        if request.stage != task_stage:
            raise HTTPException(409, '候选生成阶段与采用阶段不一致')
    if request.stage == 'finish' and build_project_state(project_id).get('reviews', {}).get(request.asset_id, {}).get('sample', {}).get('status') != 'approved':
        raise HTTPException(409, '请先采用样片，再采用精细版')
    if candidate != adoption_candidate_path(str(registered)) or not candidate.is_file():
        raise HTTPException(409, '候选不是当前登记的实际文件，请重新读取作品')
    result = task.get('result') or {}
    outputs = ([item['path'] for item in result.get('outputs', []) if isinstance(item, dict) and item.get('asset_id') == request.asset_id]
               if material_kind_expected == 'media_operation' else
               result.get('published_outputs') or ([result['output']] if result.get('output') else [f'output/{value}' for value in result.get('outputs', [])]))
    if result.get('status') not in {'success', 'succeeded'} or not any(candidate == adoption_candidate_path(str(value)) for value in outputs):
        raise HTTPException(409, '成功回执未包含当前候选，不能采用')
    return persist_review(ReviewRecordRequest(
        asset_id=request.asset_id, stage=request.stage, status='approved',
        candidate_ref=request.candidate_ref, adopted_variant=adopted_variant, note=request.note,
        expected_revision=request.expected_review_revision, source='explicit-adoption',
    ), project_id)


@app.post("/api/project-state/checkpoints")
def save_checkpoint(request: CheckpointRequest) -> dict[str, Any]:
    return persist_checkpoint(request)


@app.post("/api/projects/{project_id}/checkpoints")
def save_project_checkpoint(project_id: str, request: ProjectCheckpointRequest) -> dict[str, Any]:
    require_project_manifest(project_id)
    return persist_checkpoint(request, project_id)


def persist_checkpoint(request: CheckpointRequest, project_id: str | None = None) -> dict[str, Any]:
    if request.status not in {"draft", "pending_review", "approved", "changes_requested", "stale"}:
        raise HTTPException(422, "不支持的 checkpoint 状态")
    data = {"stage_id": request.stage_id, "status": request.status, "note": request.note, "upstream_revisions": request.upstream_revisions}
    record = append_project_record(f"checkpoint:{request.stage_id}", data, asset_id=request.asset_id, source=request.source, project_id=project_id, expected_revision=request.expected_revision)
    return {**data, "asset_id": request.asset_id, "revision": record["revision"], "source": record["source"], "created_at": record["created_at_iso"]}


@app.post("/api/project-state/artifacts")
def save_artifact(request: ArtifactRequest) -> dict[str, Any]:
    return persist_artifact(request)


@app.post("/api/projects/{project_id}/artifacts")
def save_project_artifact(project_id: str, request: ProjectArtifactRequest) -> dict[str, Any]:
    require_project_manifest(project_id)
    return persist_artifact(request, project_id)


class ScriptScene(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    text: str | None = Field(default=None, max_length=500000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)

    @field_validator('id', 'title')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('场次编号与标题不能为空')
        if value != value.strip():
            raise ValueError('场次编号与标题不能含首尾空白')
        return value


class ScriptUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    title: str = Field(default='', max_length=240)
    text: str = Field(max_length=2000000)
    target_duration_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    scenes: list[ScriptScene] = Field(default_factory=list, max_length=1000)


def latest_script(project_id: str) -> dict[str, Any] | None:
    return next((record for record in reversed(project_records(project_id))
                 if record['record_type'] == 'artifact:script' and record['asset_id'] == ''), None)


def script_document(project_id: str) -> dict[str, Any]:
    record = latest_script(project_id)
    data = record['data'] if record else {}
    content = data.get('content') or {}
    return {'project_id': project_id, 'title': data.get('title', ''),
            'text': content.get('text', ''), 'target_duration_seconds': content.get('target_duration_seconds'),
            'scenes': content.get('scenes', []), 'revision': record['revision'] if record else 0,
            'status': data.get('status', 'draft')}


def validate_script_content(project_id: str, content: dict[str, Any]) -> None:
    from pydantic import ValidationError
    try:
        parsed = ScriptUpdateRequest(expected_revision=0, text=content.get('text', ''),
                                     target_duration_seconds=content.get('target_duration_seconds'),
                                     scenes=content.get('scenes', []))
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    scene_ids = [scene.id for scene in parsed.scenes]
    if len(scene_ids) != len(set(scene_ids)):
        raise HTTPException(422, '场次编号必须唯一')
    plan, _ = load_project_plan(project_id)
    dangling = [segment['id'] for segment in plan['segments'] if isinstance(segment, dict)
                and segment.get('script_scene_id') and segment['script_scene_id'] not in scene_ids]
    if dangling:
        raise HTTPException(409, {'code': 'script_scene_in_use', 'message': '场次仍关联分镜，请先解除关联或重新分配场次',
                                  'segment_ids': dangling})


def validate_script_link(project_id: str, scene_id: str, expected_revision: int | None) -> dict[str, Any]:
    script = script_document(project_id)
    if expected_revision is None:
        raise HTTPException(422, '关联或解除场次需要 expected_script_revision')
    if expected_revision != script['revision']:
        raise HTTPException(409, {'code': 'script_revision_conflict', 'message': '剧本已修改，请重新读取后关联场次',
                                  'current_revision': script['revision'], 'expected_revision': expected_revision})
    if scene_id and scene_id not in {scene['id'] for scene in script['scenes']}:
        raise HTTPException(422, '剧本场次不存在')
    return {'script_scene_id': scene_id, 'script_revision': script['revision'] if scene_id else None}


@app.get('/api/projects/{project_id}/script')
def get_project_script(project_id: str) -> dict[str, Any]:
    require_project_manifest(project_id)
    return script_document(project_id)


@app.put('/api/projects/{project_id}/script')
@plan_transaction
def update_project_script(project_id: str, request: ScriptUpdateRequest) -> dict[str, Any]:
    require_project_manifest(project_id)
    previous = latest_script(project_id)
    data = previous['data'] if previous else {}
    content = {**(data.get('content') or {}), 'text': request.text,
               'scenes': [scene.model_dump(exclude_none=True) for scene in request.scenes]}
    if 'target_duration_seconds' in request.model_fields_set:
        content['target_duration_seconds'] = request.target_duration_seconds
    persist_artifact(ArtifactRequest(kind='script', title=request.title, content=content,
                                     references=data.get('references', []), status='draft',
                                     expected_revision=request.expected_revision), project_id)
    return script_document(project_id)


@plan_transaction
def persist_artifact(request: ArtifactRequest, project_id: str | None = None) -> dict[str, Any]:
    if request.status not in {"draft", "pending_review", "approved", "changes_requested", "stale"}:
        raise HTTPException(422, "不支持的工件状态")
    if request.kind == 'script' and not request.asset_id:
        validate_script_content(project_id or CURRENT_PROJECT_ID, request.content)
    data = {"title": request.title, "content": request.content, "references": request.references, "status": request.status}
    record = append_project_record(f"artifact:{request.kind}", data, asset_id=request.asset_id, source=request.source, project_id=project_id, expected_revision=request.expected_revision)
    return {"kind": request.kind, "asset_id": request.asset_id, **data, "revision": record["revision"], "source": record["source"], "created_at": record["created_at_iso"]}


@app.get('/api/private/series')
def list_private_series() -> dict[str, Any]:
    if PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    return {'series': PRIVATE_WORKSPACES.series_tree()}


@app.post('/api/private/series', status_code=201)
def create_private_series(request: SeriesCreateRequest) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    return PRIVATE_WORKSPACES.create_series(request.name)


@app.get('/api/private/series/{series_name}/works/{work_name}/media')
def list_private_archive_media(series_name: str, work_name: str) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    try:
        return PRIVATE_WORKSPACES.archive_media(series_name, work_name)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(404, '目录不存在') from exc


def private_delete_target(series_name: str, work_name: str | None = None) -> tuple[Path, Path]:
    if PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    try:
        return PRIVATE_WORKSPACES.deletion_target(series_name, work_name)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(404, '目录不存在') from exc


@app.post('/api/private/series/{series_name}/deletion-plan')
def plan_series_deletion(series_name: str) -> dict[str, Any]:
    root, target = private_delete_target(series_name)
    return private_deletion.plan(root, target, RUNTIME / 'deletion-plans')


@app.delete('/api/private/series/{series_name}')
def delete_series(series_name: str, request: DirectoryDeleteRequest) -> dict[str, Any]:
    root, target = private_delete_target(series_name)
    if request.confirm != series_name:
        raise HTTPException(422, '请填写完整系列名称以确认删除')
    ensure_no_release_blocking_tasks('删除系列')
    result = private_deletion.apply(root, target, RUNTIME / 'deletion-plans', request.plan_id)
    PRIVATE_WORKSPACES.retire_deleted(series_name)
    return result


@app.post('/api/private/series/{series_name}/works/{work_name}/deletion-plan')
def plan_work_deletion(series_name: str, work_name: str) -> dict[str, Any]:
    root, target = private_delete_target(series_name, work_name)
    return private_deletion.plan(root, target, RUNTIME / 'deletion-plans')


@app.delete('/api/private/series/{series_name}/works/{work_name}')
def delete_work(series_name: str, work_name: str, request: DirectoryDeleteRequest) -> dict[str, Any]:
    root, target = private_delete_target(series_name, work_name)
    if request.confirm != work_name:
        raise HTTPException(422, '请填写完整作品目录名称以确认删除')
    ensure_no_release_blocking_tasks('删除作品')
    result = private_deletion.apply(root, target, RUNTIME / 'deletion-plans', request.plan_id)
    PRIVATE_WORKSPACES.retire_deleted(series_name, work_name)
    return result


@app.get("/api/projects")
def projects() -> dict[str, Any]:
    catalog = load_project_catalog()
    manifests: list[dict[str, Any]] = []
    principal = current_principal.get()
    selected = PRIVATE_WORKSPACES.selected() if PRIVATE_WORKSPACES is not None and principal else CURRENT_PROJECT_ID
    for entry in project_entries():
        project_id = str(entry.get("id", ""))
        if PRIVATE_WORKSPACES is not None and principal:
            try:
                PRIVATE_WORKSPACES.require(project_id)
            except HTTPException:
                continue
        try:
            manifest = load_project_manifest(project_id)
        except RuntimeError:
            continue
        summary = project_summary(manifest)
        summary["selected"] = project_id == selected
        manifests.append(summary)
    return {"schema_version": 1, "default_project_id": selected if principal else catalog.get("default_project_id"), "current_project_id": selected, "projects": manifests}


@app.post("/api/projects/select")
@catalog_transaction
def select_project(request: ProjectSelectRequest) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        manifest = require_project_manifest(request.project_id)
        PRIVATE_WORKSPACES.select(request.project_id)
        return public_project(manifest)
    ensure_no_release_blocking_tasks("切换项目")
    try:
        manifest = load_project_manifest(request.project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    apply_project_manifest(manifest)
    catalog = load_project_catalog()
    catalog["default_project_id"] = request.project_id
    PROJECT_CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return public_project()


def require_project_manifest(project_id: str) -> dict[str, Any]:
    try:
        return load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}")
def project_detail(project_id: str) -> dict[str, Any]:
    """Read a work without changing the shared legacy selection."""
    return public_project(require_project_manifest(project_id))


@app.delete('/api/projects/{project_id}/registration')
def retire_legacy_project(project_id: str) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is None:
        raise HTTPException(404, '私人工作区未启用')
    principal = current_principal.get()
    if principal is None or not principal.administrator:
        raise HTTPException(404, '作品不存在')
    require_project_manifest(project_id)
    ensure_no_release_blocking_tasks('移除旧作品登记')
    PRIVATE_WORKSPACES.retire_legacy_registration(project_id)
    return {'retired_project_id': project_id, 'source_files_preserved': True}


@app.get("/api/projects/{project_id}/plan")
def project_plan(project_id: str) -> dict[str, Any]:
    return load_project_plan(project_id)[0]


@app.get("/api/projects/{project_id}/state")
def project_state_detail(project_id: str) -> dict[str, Any]:
    require_project_manifest(project_id)
    state = build_project_state(project_id)
    state['readiness'] = project_readiness(project_id, state)
    return state


@app.get("/api/projects/{project_id}/tasks")
def project_task_list(project_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    require_project_manifest(project_id)
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (project_id, limit + 1, offset)).fetchall()
    more = len(rows) > limit
    values = [task_row(row) for row in rows[:limit]]
    for task in values:
        task['queue'] = TASK_SCHEDULER.queue_info(task['id'])
    return {"project_id": project_id, "tasks": values, "next_offset": offset + limit if more else None}


@app.get("/api/projects/{project_id}/tasks/{task_id}")
def project_task_detail(project_id: str, task_id: str) -> dict[str, Any]:
    require_project_manifest(project_id)
    task = get_task(task_id)
    if not task or task.get("project_id") != project_id:
        raise HTTPException(404, "该作品中不存在此任务")
    return task


def task_video_path(task: dict[str, Any]) -> str | None:
    result = task.get("result") or {}
    if not isinstance(result, dict):
        return None
    published = result.get("published_outputs") or []
    if published:
        return str(published[0])
    if result.get("output"):
        return str(result["output"])
    outputs = result.get("outputs") or []
    if outputs:
        return f"output/{str(outputs[0]).replace(chr(92), '/').lstrip('/')}"
    return None


def public_version_snapshot(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = json.loads(json.dumps(snapshot, ensure_ascii=False))
    execution = visible.get("execution_snapshot")
    if not isinstance(execution, dict):
        execution = payload.get("execution_snapshot")
        if isinstance(execution, dict):
            visible["execution_snapshot"] = execution
    if not isinstance(execution, dict):
        execution = {}
    if execution.get("prompt") is not None:
        visible["prompt"] = execution["prompt"]
    elif "prompt" not in visible:
        prompt = (payload.get("pipeline_values") or {}).get("prompt") or payload.get("prompt")
        if prompt is not None:
            visible["prompt"] = prompt
    parameters = execution.get("parameters") or {}
    if "duration_seconds" not in visible and isinstance(parameters, dict):
        duration = parameters.get("duration_seconds") or parameters.get("duration")
        if duration is not None:
            visible["duration_seconds"] = duration
    frames = visible.get("keyframes") or {}
    audio = visible.get("audio") or {}
    materials = execution.get("materials") or {}
    if not isinstance(materials, dict):
        materials = {}
    if not isinstance(frames, dict):
        frames = {}
    if not isinstance(audio, dict):
        audio = {}
    first = materials.get("first-frame") or materials.get("first_frame") or frames.get("first") or payload.get("first_frame")
    last = materials.get("last-frame") or materials.get("last_frame") or frames.get("last") or payload.get("last_frame")
    guide = materials.get("guide") or materials.get("audio_guide") or audio.get("guide") or payload.get("audio_guide")
    for key, value in (("first_frame", first), ("last_frame", last), ("audio_guide", guide)):
        if value is not None:
            visible[key] = value
    return visible


def same_registered_media(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    try:
        return resolve_registered_path(first) == resolve_registered_path(second)
    except (ValueError, OSError):
        return False


def segment_versions(project_id: str, segment_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document, _ = load_project_plan(project_id)
    segment = next((item for item in document["segments"] if isinstance(item, dict) and item.get("id") == segment_id), None)
    if segment is None:
        raise HTTPException(404, "分镜不存在")
    history = {str(item.get("task_id")): item for item in segment.get("generation_history", [])
               if isinstance(item, dict) and item.get("task_id")}
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE project_id=? AND asset_id=? AND status='succeeded' ORDER BY updated_at DESC, id DESC",
            (project_id, segment_id),
        ).fetchall()
    versions = []
    for row in rows:
        task = task_row(row)
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task(task)
        payload = task.get("payload") or {}
        if payload.get("kind") not in (None, "workflow") or payload.get("source_plan"):
            continue
        recorded = history.get(task["id"]) or {}
        video_path = recorded.get("video_path") or task_video_path(task)
        if not video_path:
            continue
        generated_at = float(recorded.get("generated_at") or task["updated_at"])
        snapshot = recorded.get("snapshot") or payload.get("segment_snapshot") or {}
        snapshot = public_version_snapshot(snapshot if isinstance(snapshot, dict) else {}, payload)
        current_task_id = segment.get("current_version_task_id")
        current_video = (segment.get("video") or {}).get("path")
        is_current = (current_task_id == task["id"] if current_task_id else
                      str(segment.get("comfyui_task_id") or "") == str(task.get("prompt_id") or task["id"])
                      and same_registered_media(current_video, video_path))
        versions.append({
            "task_id": task["id"], "prompt_id": task.get("prompt_id"),
            "generated_at": system_time_iso(generated_at), "video_path": video_path,
            "snapshot": snapshot, "snapshot_complete": bool(recorded.get("snapshot") or payload.get("segment_snapshot")),
            "is_current": is_current,
            "restored_at": system_time_iso(segment["current_version_restored_at"])
            if is_current and segment.get("current_version_restored_at") else None,
        })
    versions.sort(key=lambda item: item["generated_at"], reverse=True)
    return segment, versions


@app.get("/api/projects/{project_id}/segments/{segment_id}/versions")
def project_segment_versions(
    project_id: str, segment_id: str,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    segment, versions = segment_versions(project_id, segment_id)
    selected = versions[offset:offset + limit]
    manifest = require_project_manifest(project_id)
    document, _ = load_project_plan(project_id)
    current = next((item for item in versions if item['is_current']), None)
    for item in selected:
        facts = authorized_media_facts(item['video_path'])
        item['media'] = facts
        item['parameter_differences'] = creative_inspection.version_differences(
            current['snapshot'] if current else {}, item['snapshot'])
    inspection = inspect_shot(manifest, document, segment)
    reference = inspection['reference_comparison']['reference'] or {}
    source_range = (segment.get('source_range_seconds') or [0, None]) if reference.get('source') == 'analysis' else [0, None]
    reference_facts = reference.get('facts') or {}
    offset_seconds = float(source_range[0] or 0)
    reference_seconds = reference_facts.get('duration_seconds')
    reference_interval = (max(0, min(reference_seconds, float(source_range[1])) - offset_seconds)
                          if reference_seconds is not None and source_range[1] is not None else
                          max(0, reference_seconds - offset_seconds) if reference_seconds is not None else None)
    comparison_sources = [
        {'id': 'current', 'title': '当前候选', 'path': (segment.get('video') or {}).get('path'),
         'facts': inspection['timing']['observed_candidate'], 'offset_seconds': 0,
         'duration_seconds': inspection['timing']['observed_candidate'].get('duration_seconds')},
        {'id': 'reference', 'title': reference.get('title') or '参考视频（未选择）', 'path': reference.get('path'),
         'facts': reference_facts, 'offset_seconds': offset_seconds, 'duration_seconds': reference_interval},
        *[{'id': item['task_id'], 'title': f"历史 {item['generated_at']} · {item['task_id'][:8]}",
           'path': item['video_path'], 'facts': item['media'], 'offset_seconds': 0,
           'duration_seconds': item['media'].get('duration_seconds')} for item in selected],
    ]
    return {
        "segment_id": segment_id, "current_version_task_id": segment.get("current_version_task_id"),
        "items": selected, "total": len(versions),
        "next_offset": offset + limit if offset + limit < len(versions) else None,
        'revision': document.get('revision', 0),
        'inspection': inspection, 'comparison_sources': comparison_sources,
        'change_impact': creative_inspection.change_impact(document, segment_id),
        'review_records': [record for record in project_records(project_id)
                           if record.get('asset_id') == segment_id and record.get('record_type', '').startswith('review:')],
    }


@app.post("/api/projects/{project_id}/segments/{segment_id}/versions/{task_id}/restore")
@plan_transaction
def restore_project_segment_version(
    project_id: str, segment_id: str, task_id: str, request: SegmentVersionRestoreRequest,
) -> dict[str, Any]:
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_plan_revision)
    segment = next((item for item in document["segments"] if isinstance(item, dict) and item.get("id") == segment_id), None)
    if segment is None:
        raise HTTPException(404, "分镜不存在")
    task = get_task(task_id)
    if not task or task.get("project_id") != project_id or task.get("asset_id") != segment_id or task.get("status") != "succeeded":
        raise HTTPException(404, "该分镜没有对应的成功生成版本")
    payload = task.get("payload") or {}
    if payload.get("kind") not in (None, "workflow") or payload.get("source_plan"):
        raise HTTPException(409, "此任务不是分镜视频生成版本")
    recorded = next((item for item in segment.get("generation_history", [])
                     if isinstance(item, dict) and item.get("task_id") == task_id), {})
    video_path = str(recorded.get("video_path") or task_video_path(task) or "")
    task_path = task_video_path(task)
    if not video_path or not task_path:
        raise HTTPException(409, "历史版本缺少生成文件回执")
    try:
        video = resolve_registered_path(video_path)
        receipt_video = resolve_registered_path(task_path)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, "历史版本文件不在当前作品允许的工作区") from exc
    if video != receipt_video or not video.is_file():
        raise HTTPException(409, "历史版本文件与生成回执不一致或已丢失")
    snapshot = recorded.get("snapshot") or payload.get("segment_snapshot") or {}
    if isinstance(snapshot, dict):
        for key in SEGMENT_GENERATION_FIELDS:
            if key in snapshot:
                segment[key] = json.loads(json.dumps(snapshot[key], ensure_ascii=False))
            elif key in ('generation', 'resolved_generation'):
                # Older candidates predate automatic creation. Restoring one
                # must not retain settings from a later automatic version.
                segment.pop(key, None)
    timestamp = time.time()
    segment.update(
        status="video_generated", video={"path": video_path, "variant": "v3-A"},
        comfyui_task_id=recorded.get("prompt_id") or task.get("prompt_id") or task_id,
        current_version_task_id=task_id, current_version_restored_at=timestamp,
        notes="已从历史记录恢复为当前版本。",
    )
    persist_plan_edit(project_id, document, plan_path)
    return {"revision": document["revision"], "segment": segment, "restored_at": system_time_iso(timestamp)}


@app.post("/api/projects/{project_id}/segments/{segment_id}/recipe-identity/repair")
@gpu_admission
@plan_transaction
def repair_project_recipe_identity(project_id: str, segment_id: str, request: RecipeIdentityRepairRequest) -> dict[str, Any]:
    """Explicit, receipt-proven repair of the frozen-path overwrite defect only.

    This does not restore a candidate, reinterpret inputs, reinstall a recipe,
    invalidate reviews or accept arbitrary client-provided workflow paths.
    """
    manifest = require_project_manifest(project_id)
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    task = project_task_detail(project_id, request.task_id)
    segment = next((row for row in document['segments'] if row.get('id') == segment_id), None)
    if segment is None or task.get('asset_id') != segment_id:
        raise HTTPException(404, '该分镜没有对应生成回执')
    payload = task.get('payload') or {}
    execution = payload.get('execution_snapshot') or {}
    snapshot = payload.get('segment_snapshot') or {}
    original = snapshot.get('workflow')
    _, stage_id = select_preset_execution(manifest, original, None)
    history = next((row for row in segment.get('generation_history', []) if row.get('task_id') == request.task_id), {})
    if (task.get('status') != 'succeeded' or payload.get('kind') not in (None, 'workflow') or payload.get('source_plan')
            or segment.get('current_version_task_id') != request.task_id or not stage_id
            or execution.get('pipeline_stage_id') != stage_id or not history
            or history.get('workflow') != payload.get('workflow')):
        raise HTTPException(409, '无法从当前成功任务及已登记方案证明配方身份，请保留现状并核对回执')
    try:
        source = resolve_registered_path(str(original))
        frozen = resolve_registered_path(str(payload.get('workflow')))
        if (source != resolve_registered_path(str(execution.get('source_workflow')))
                or frozen != resolve_registered_path(str(execution.get('api_graph')))
                or not source.is_file() or not frozen.is_file()
                or frozen.parent != task_snapshot_root(project_id).resolve()):
            raise ValueError('来源或快照不一致')
        source_graph = json.loads(source.read_text(encoding='utf-8-sig'))
        frozen_graph = json.loads(frozen.read_text(encoding='utf-8-sig'))
        if {key: node['class_type'] for key, node in source_graph.items()} != {key: node['class_type'] for key, node in frozen_graph.items()}:
            raise ValueError('图节点来源不一致')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(409, '配方或冻结图来源校验失败，未改写计划') from exc
    if any(segment.get(key) != snapshot.get(key) for key in SEGMENT_GENERATION_FIELDS if key != 'workflow'):
        raise HTTPException(409, '生成后创作输入已改变，不能用旧回执恢复配方身份')
    current = segment.get('workflow')
    if current not in (original, payload.get('workflow'), root_relative_path(frozen)):
        raise HTTPException(409, '当前配方不是该任务写入的快照，未改写计划')
    with DB_LOCK, db() as connection:
        placeholders = ','.join('?' for _ in RELEASE_BLOCKING_STATUSES)
        active = connection.execute(f'SELECT id FROM tasks WHERE project_id=? AND status IN ({placeholders}) LIMIT 1',
                                    (project_id, *RELEASE_BLOCKING_STATUSES)).fetchone()
    if active:
        raise HTTPException(409, '作品仍有执行或待核对任务，稍后再修复身份')
    changed = current != original
    receipt = {'asset_id': segment_id, 'task_id': request.task_id, 'previous_workflow': current,
               'workflow': original, 'stage_id': stage_id, 'expected_revision': request.expected_revision}
    if changed and not request.dry_run:
        segment['workflow'] = original
        document.setdefault('recipe_identity_repairs', []).append({**receipt, 'repaired_at': time.time()})
        # A metadata identity correction must preserve creative/review state.
        write_plan_version(plan_path, document)
    return {**receipt, 'dry_run': request.dry_run, 'changed': changed and not request.dry_run,
            'repair_needed': changed, 'revision': document.get('revision', 0)}


@app.post("/api/projects/create")
@catalog_transaction
def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        with PRIVATE_WORKSPACES.lock:
            project_id, workspace = PRIVATE_WORKSPACES.allocate(request.project_id or request.title, series=request.series, title=request.title)
            for folder in ('plans', 'assets/image', 'assets/audio', 'assets/video', 'workflows/ui', 'workflows/api', 'records/prompts', 'records/tasks', 'records/reviews'):
                (workspace / folder).mkdir(parents=True, exist_ok=True)
            manifest = blank_project_manifest(project_id, request.series, request.title, workspace)
            manifest['default_creation_mode'] = request.creation_mode
            (workspace / 'director.project.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            persist_project_manifest(manifest, make_default=False)
            if request.select:
                PRIVATE_WORKSPACES.select(project_id)
        return {'project': public_project(manifest), 'created': True, 'workspace': root_relative_path(workspace)}
    ensure_no_release_blocking_tasks("创建项目")
    project_id = unique_project_id(request.project_id or request.title)
    nested = DIRECTOR_WORKSPACES_ROOT == STORAGE_ROOTS['workspaces']
    workspace = (DIRECTOR_WORKSPACES_ROOT / (project_location(request.series, request.title, project_id).relative_to('series') if nested else Path(project_id))).resolve()
    suffix = 2
    while workspace.exists():
        project_id = unique_project_id(f"{request.project_id or request.title}-{suffix}")
        workspace = (DIRECTOR_WORKSPACES_ROOT / (project_location(request.series, request.title, project_id).relative_to('series') if nested else Path(project_id))).resolve()
        suffix += 1
    workspace.mkdir(parents=True, exist_ok=False)
    for folder in ("plans", "assets/image", "assets/audio", "assets/video", "workflows/ui", "workflows/api", "records/prompts", "records/tasks", "records/reviews"):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    manifest = blank_project_manifest(project_id, request.series, request.title, workspace)
    manifest['default_creation_mode'] = request.creation_mode
    (workspace / "director.project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_project_manifest(manifest, make_default=request.select)
    if request.select:
        apply_project_manifest(manifest)
    return {"project": public_project(manifest), "created": True, "workspace": root_relative_path(workspace)}


@catalog_transaction
def save_project_manifest(manifest: dict[str, Any]) -> None:
    """Persist a project manifest and its workspace copy after an in-app edit."""
    manifest_path = PROJECTS_ROOT / f"{manifest['id']}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    workspace_value = manifest.get("workspace_root")
    if workspace_value:
        workspace = resolve_registered_path(str(workspace_value))
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "director.project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_project_manifest(manifest, make_default=(str(manifest.get("id")) == CURRENT_PROJECT_ID))


@app.get("/api/production-presets")
def production_preset_catalog() -> dict[str, Any]:
    return {"presets": [production_presets.catalog(ROOT / "ComfyUI-Shared" / "models", preset_id) for preset_id in production_presets.PRESET_IDS]}


@app.post("/api/projects/{project_id}/production-presets/{preset_id}")
@gpu_admission
def install_production_preset(project_id: str, preset_id: str) -> dict[str, Any]:
    ensure_no_release_blocking_tasks("配置制作方案")
    if preset_id not in production_presets.PRESET_IDS:
        raise HTTPException(404, "制作方案不存在")
    manifest = require_project_manifest(project_id)
    result = configure_production_preset(manifest, preset_id)
    if manifest.get('default_creation_mode') == 'auto':
        manifest['default_creation_mode'] = 'advanced'
        save_project_manifest(manifest)
        result['project'] = public_project(manifest)
    return result


def configure_production_preset(manifest: dict[str, Any], preset_id: str, *, automatic=False) -> dict[str, Any]:
    """Reuse recipe configuration; automatic additions never overwrite an installed stage."""
    project_id = str(manifest['id'])
    if (manifest.get("production_preset") or {}).get("id") == preset_id:
        return {"project": public_project(manifest), "already_installed": True}
    stages = manifest.get("pipeline") or []
    if automatic and any(item.get('id') == production_presets.stage_id(preset_id) for item in stages):
        return {'project': public_project(manifest), 'already_installed': True}
    if any(item.get("id") == production_presets.preset_stage_id(preset_id) for item in stages):
        raise HTTPException(409, "项目已有视频制作步骤，不能覆盖其执行配置")
    try:
        graph = production_presets.load_recipe(preset_id)
        specs = production_presets.input_specs(preset_id)
        for spec in specs:
            binding = spec["binding"]
            graph[binding["node_id"]]["inputs"][binding["input"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"制作方案不可用：{exc}") from exc
    workspace = resolve_registered_path(str(manifest["workspace_root"]))
    destination = workspace / "workflows" / "api" / f"{preset_id}-{uuid.uuid4().hex[:10]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Use project identifiers rather than user-entered filesystem paths.
    graph["92"]["inputs"]["filename_prefix"] = f"导演工作台工作目录/{project_id}/video/h3"
    graph["92"]["_meta"] = {"director_preset": preset_id}
    destination.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    workflow = root_relative_path(destination)
    manifest["pipeline"] = [*stages, production_presets.stage(workflow, len(stages) + 1, preset_id)]
    if not automatic or not manifest.get('production_preset'):
        manifest["production_preset"] = {"id": preset_id, "title": production_presets.title(preset_id), "workflow": workflow}
    save_project_manifest(manifest)
    if project_id == CURRENT_PROJECT_ID:
        apply_project_manifest(manifest)
    return {"project": public_project(manifest), "already_installed": False}


def project_default_workflow() -> str | None:
    return (CURRENT_PROJECT.get("production_preset") or {}).get("workflow")


def resolve_automatic_shot(manifest, segment):
    settings = segment.get('generation') or {}
    if settings.get('mode') != 'auto':
        segment.pop('resolved_generation', None)
        return
    try:
        h3_duration.plan(segment.get('duration_seconds'), settings['aspect_ratio'])
        if not str(segment.get('prompt') or '').strip():
            raise ValueError('请填写画面与动作描述。')
        frames, audio = dict(segment.get('keyframes') or {}), dict(segment.get('audio') or {})
        selected = shot_creation.resolve(settings, first=frames.get('first'), last=frames.get('last'), guide=audio.get('guide'))
        # Resolve only explicitly bound controls, under the existing owner checks.
        for key in ('first', 'last'):
            if frames.get(key):
                frames[key] = resolve_pipeline_material(frames[key], '图片', manifest)
        if audio.get('guide'):
            audio['guide'] = resolve_pipeline_material(audio['guide'], '音频', manifest)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, {'code': 'unsupported_creation_inputs', 'message': str(exc)}) from exc
    configure_production_preset(manifest, selected['preset_id'], automatic=True)
    stage = next(row for row in manifest['pipeline'] if row['id'] == production_presets.stage_id(selected['preset_id']))
    segment.update(keyframes=frames, audio=audio, workflow=stage['execution']['references'][0], resolved_generation=selected)


def select_preset_execution(project: dict[str, Any], workflow: str | None, requested_stage: str | None) -> tuple[str | None, str | None]:
    """Keep installed first-only and first/last recipes independently selectable."""
    known_stages = {production_presets.preset_stage_id(key) for key in production_presets.PRESET_IDS}
    if requested_stage in (None, 'auto') and workflow:
        matched = next((stage for stage in project.get('pipeline', []) if stage.get('id') in known_stages and
                        workflow in ((stage.get('execution') or {}).get('references') or [])), None)
        if matched:
            return workflow, str(matched['id'])
    if requested_stage == 'auto':
        raise HTTPException(422, '请先保存描述、剧情时长和画幅。')
    if requested_stage in known_stages:
        stage = next((item for item in project.get('pipeline', []) if item.get('id') == requested_stage), None)
        if stage:
            references = (stage.get('execution') or {}).get('references') or []
            if len(references) != 1:
                raise HTTPException(422, '制作方案须登记唯一执行图')
            return str(references[0]), requested_stage
    preset = project.get('production_preset') or {}
    if not requested_stage and workflow and workflow == preset.get('workflow'):
        return workflow, production_presets.preset_stage_id(preset.get('id', production_presets.PRESET_ID))
    return workflow, requested_stage


@app.post("/api/projects/{project_id}/pipeline/stages")
def create_pipeline_stage(project_id: str, request: PipelineStageCreateRequest) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    stages = manifest.get("pipeline") if isinstance(manifest.get("pipeline"), list) else []
    base_id = "".join(char.lower() if char.isalnum() else "-" for char in request.title).strip("-")[:60] or "stage"
    known = {str(item.get("id")) for item in stages if isinstance(item, dict)}
    stage_id = base_id
    suffix = 2
    while stage_id in known:
        stage_id = f"{base_id}-{suffix}"
        suffix += 1
    mode = "comfyui" if "ComfyUI" in request.backend else "script" if "脚本" in request.backend else "hybrid"
    inputs = [value.strip() for value in request.inputs if value.strip()]
    outputs = [value.strip() for value in request.outputs if value.strip()]
    stage: dict[str, Any] = {
        "id": stage_id, "order": len(stages) + 1, "title": request.title.strip(),
        "purpose": request.purpose.strip() or "登记该制作步骤的输入、输出和审核结果。",
        "backend": request.backend.strip() or "混合流程", "status": "待接入",
        "inputs": inputs, "outputs": outputs, "recipe": request.workflow.strip() or "待登记工作流或脚本",
        "note": "该步骤由导演台登记；实际执行仍交给 ComfyUI 或项目适配器。",
        "input_specs": [{"id": f"input-{index}", "label": label, "kind": pipeline_value_kind(label), "required": True, "description": f"提交“{request.title.strip()}”前需要准备的输入。", **pipeline_control(label)} for index, label in enumerate(inputs, start=1)],
        "output_specs": [{"id": f"output-{index}", "label": label, "kind": pipeline_value_kind(label), "description": f"“{request.title.strip()}”完成后登记的结果。", "state": "待接入"} for index, label in enumerate(outputs, start=1)],
        "execution": {"mode": mode, "label": request.workflow.strip() or request.backend.strip() or "待接入执行器", "references": [], "state": "待接入"},
    }
    stages.append(stage)
    manifest["pipeline"] = stages
    save_project_manifest(manifest)
    if project_id == CURRENT_PROJECT_ID:
        apply_project_manifest(manifest)
    return {"project": public_project(manifest), "stage": stage}


def record_segment_artifacts(project_id: str, segment: dict[str, Any], source: str = "plan-editor") -> None:
    segment_id = str(segment.get("id", ""))
    keyframes = segment.get("keyframes") if isinstance(segment.get("keyframes"), dict) else {}
    audio = segment.get("audio") if isinstance(segment.get("audio"), dict) else {}
    references = [str(value) for value in [keyframes.get("first"), keyframes.get("last"), audio.get("guide"), audio.get("delivery_master")] if value]
    append_project_record(
        "artifact:scene",
        {"title": f"{segment_id} 场景", "content": {key: segment.get(key) for key in ("location", "shot_size", "camera", "wardrobe")}, "references": references[:2], "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )
    append_project_record(
        "artifact:performance",
        {"title": f"{segment_id} 动作表演", "content": {"performance": segment.get("performance"), "audio": audio}, "references": references[2:], "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )
    append_project_record(
        "artifact:storyboard",
        {"title": f"{segment_id} 分镜", "content": {key: segment.get(key) for key in ("start_seconds", "end_seconds", "duration_seconds", "prompt", "workflow", "dependencies")}, "references": references, "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )


@app.post("/api/projects/{project_id}/plan/segments")
@plan_transaction
def create_plan_segment(project_id: str, request: PlanSegmentCreateRequest) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    principal = current_principal.get()
    if principal and not principal.administrator and request.workflow:
        if request.workflow != (manifest.get('production_preset') or {}).get('workflow'):
            raise HTTPException(403, '工作流由管理员维护，请选择已配置的制作方案')
    plan_value = manifest.get("plan_path")
    if not plan_value:
        raise HTTPException(422, "项目没有可写入的计划路径")
    try:
        plan_path = resolve_registered_path(str(plan_value))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    document: dict[str, Any] = {"schema_version": 1, "project": project_id, "status": "draft", "segments": [], "assembly": None}
    if plan_path.is_file():
        try:
            loaded = json.loads(plan_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                document.update(loaded)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, f"计划文件无法读取: {plan_path}") from exc
    check_plan_revision(document, request.expected_revision)
    segments = document.get("segments") if isinstance(document.get("segments"), list) else []
    next_start = sum(float(item.get("duration_seconds", 0) or 0) for item in segments if isinstance(item, dict))
    base_id = request.segment_id or f"S{len(segments) + 1:02d}"
    known = {str(item.get("id")) for item in segments if isinstance(item, dict)}
    if manifest.get('assembly_asset_id'):
        known.add(str(manifest['assembly_asset_id']))
    segment_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in base_id).strip("-")[:80] or f"S{len(segments) + 1:02d}"
    normalized_base = segment_id
    suffix = 2
    while segment_id in known:
        ending = f"-{suffix}"
        segment_id = normalized_base[:80 - len(ending)] + ending
        suffix += 1
    segment: dict[str, Any] = {
        "id": segment_id, "start_seconds": round(next_start, 3), "end_seconds": round(next_start + request.duration_seconds, 3),
        "duration_seconds": request.duration_seconds, "status": "planned", "location": request.location.strip(),
        "shot_size": request.shot_size.strip(), "camera": request.camera.strip(), "wardrobe": request.wardrobe.strip(),
        "performance": request.performance.strip(), "prompt": (request.prompt or "").strip() or None, "workflow": (request.workflow or "").strip() or None,
        "keyframes": {"first": request.first_frame.strip() if request.first_frame else None, "last": request.last_frame.strip() if request.last_frame else None},
    }
    if request.script_scene_id is not None:
        segment.update(validate_script_link(project_id, request.script_scene_id.strip(), request.expected_script_revision))
    if request.audio_guide or request.delivery_master:
        segment['audio'] = {key: value.strip() for key, value in (
            ('guide', request.audio_guide), ('delivery_master', request.delivery_master)
        ) if value and value.strip()}
    if request.generation is not None:
        if request.workflow and request.generation.mode == 'auto':
            raise HTTPException(422, '自动创作不接受技术工作流，请清除引用或明确使用高级模式。')
        segment['generation'] = request.generation.model_dump()
    elif manifest.get('default_creation_mode') == 'auto' and not request.workflow:
        segment['generation'] = ShotGenerationRequest().model_dump()
    resolve_automatic_shot(manifest, segment)
    validate_h3_plan_duration(manifest, segment)
    segments.append(segment)
    document["segments"] = segments
    persist_plan_edit(project_id, document, plan_path)
    record_segment_artifacts(project_id, segment, "director-ui")
    return {"project": public_project(manifest), "segment": segment, "plan": document}


def load_project_plan(project_id: str) -> tuple[dict[str, Any], Path]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        plan_path = resolve_registered_path(str(manifest.get("plan_path", "")))
        document = json.loads(plan_path.read_text(encoding="utf-8-sig")) if plan_path.is_file() else {"schema_version": 1, "project": project_id, "status": "draft", "segments": [], "assembly": None}
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"项目计划无法读取: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        raise HTTPException(422, "项目计划缺少 segments 数组")
    return document, plan_path


def authorized_media_facts(reference):
    if not reference:
        return {'known': False, 'reason': 'not_selected', 'duration_seconds': None}
    try:
        return creative_inspection.probe(resolve_registered_path(str(reference)))
    except (ValueError, OSError):
        return {'known': False, 'reason': 'media_unavailable', 'duration_seconds': None}


def shot_reference(manifest, segment):
    asset_id = segment.get('reference_asset_id')
    if asset_id:
        asset = next((row for row in manifest.get('assets', []) if row.get('id') == asset_id and row.get('kind') == 'video'), None)
        if asset is None:
            raise HTTPException(422, '参考视频须选择当前作品已登记的视频素材')
        path = (asset.get('sources') or {}).get('A')
        return {'asset_id': asset_id, 'title': asset.get('title', asset_id), 'path': path,
                'facts': authorized_media_facts(path), 'source': 'registered_asset'}
    if segment.get('source_analysis'):
        try:
            analysis, _ = read_reverse_analysis(manifest['id'], segment['source_analysis'])
            source = analysis.get('source') or {}
            return {'analysis_id': analysis['id'], 'title': source.get('original_name', analysis.get('title')),
                    'path': source.get('path'), 'facts': authorized_media_facts(source.get('path')), 'source': 'analysis'}
        except HTTPException:
            return {'facts': {'known': False}, 'source': 'analysis_unavailable'}
    return None


def assembly_trim_summary(manifest, document):
    """Read the configured builder's trim contract, not a client-side conversion."""
    try:
        builder = assembly_builder_path(manifest)
        if not builder.is_file() or builder.name != 'build_assembly_workflow.py':
            return None
        spec = importlib.util.spec_from_file_location('director_trim_inspection', builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, 'trim_spec'):
            return None
        rows = [{'segment_id': segment['id'], **module.trim_spec(segment)} for segment in document.get('segments', [])]
        return {'segments': rows, 'frame_count': sum(row['frame_count'] for row in rows),
                'playback_seconds': round(sum(row['frame_count'] / row['fps'] for row in rows), 6),
                'precision': '按当前装配构建器逐段四舍五入到24fps；末段也单独取整，实际成片另测。'}
    except (OSError, ValueError, KeyError, AttributeError):
        return None


def inspect_shot(manifest, document, segment, profile=None):
    if profile is None:
        stage = next((row for row in manifest.get('pipeline', []) if segment.get('workflow') and
                      segment['workflow'] in ((row.get('execution') or {}).get('references') or [])), None)
        preset_id = next((key for key in production_presets.PRESET_IDS if stage and stage.get('id') == production_presets.stage_id(key)), None)
        if preset_id is None and segment.get('workflow'):
            # Successful candidates keep the frozen graph, not the installed recipe path.
            # Read the same recipe metadata used by prepare_pipeline_payload; never infer
            # the model solely from the project having some H3 preset installed.
            try:
                graph = json.loads(resolve_workflow_path(str(segment['workflow'])).read_text(encoding='utf-8-sig'))
                frozen_preset = ((graph.get('92') or {}).get('_meta') or {}).get('director_preset')
                if frozen_preset in production_presets.PRESET_IDS:
                    preset_id = frozen_preset
            except (HTTPException, OSError, ValueError, AttributeError):
                pass
        if preset_id is None and not segment.get('workflow'):
            preset_id = (manifest.get('production_preset') or {}).get('id')
        if preset_id in production_presets.PRESET_IDS:
            aspect = production_presets.aspect_ratio(preset_id)
            try:
                profile = h3_duration.plan(segment.get('duration_seconds'), aspect)
                profile['generation_mode'] = 'text_to_video' if preset_id in production_presets.TEXT_PRESET_IDS else 'image_to_video'
            except ValueError:
                profile = None
    video = (segment.get('video') or {}).get('path')
    observed = authorized_media_facts(video)
    planned = ({'model': 'H3', 'width': profile['width'], 'height': profile['height'],
                'fps': profile['fps'], 'duration_seconds': profile['playback_seconds'],
                'frames': profile['frame_count']} if profile else {'model': 'unknown', 'duration_seconds': None})
    try:
        reference = shot_reference(manifest, segment)
    except HTTPException:
        reference = {'facts': {'known': False}, 'source': 'registered_asset_unavailable'}
    assembly = assembly_trim_summary(manifest, document)
    trim = next((row for row in (assembly or {}).get('segments', []) if row['segment_id'] == segment['id']), None)
    return {'revision': document.get('revision', 0), 'segment_id': segment['id'],
            'candidate_version': segment.get('current_version_task_id'),
            'reference_options': [{'id': row['id'], 'title': row.get('title', row['id'])} for row in manifest.get('assets', []) if row.get('kind') == 'video'],
            'reference_comparison': creative_inspection.comparison(reference, planned,
                 audio_policy=((document.get('assembly_edit') or {}).get('sound') or {}).get('policy', 'legacy_master' if (manifest.get('assembly') or {}).get('audio') else 'segment_native'),
                  first_frame=authorized_media_facts(None if (profile or {}).get('generation_mode') == 'text_to_video' else (profile or {}).get('first_frame') or (segment.get('keyframes') or {}).get('first'))),
            'timing': {'requested_seconds': (profile or {}).get('requested_seconds', segment.get('duration_seconds')), 'expected_candidate': profile,
                       'observed_candidate': observed, 'assembly_take': trim,
                       'observed_assembly': authorized_media_facts((document.get('assembly') or {}).get('output')),
                       'note': '请求是剧情计划；模型帧网格决定预计候选；实际媒体须测量；装配按取片范围独立对齐。'}}


def assembly_sound_selection(manifest: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    edit = document.get('assembly_edit') or {}
    sound = edit.get('sound') if isinstance(edit, dict) else None
    if not isinstance(sound, dict):
        legacy = (manifest.get('assembly') or {}).get('audio')
        return {'policy': 'legacy_master' if legacy else 'segment_native', 'audio_asset_id': None,
                'path': str(resolve_registered_path(str(legacy))) if legacy else None,
                'duration_seconds': None}
    if sound.get('policy') in {'segment_native', 'mute'}:
        return {'policy': sound['policy'], 'audio_asset_id': None, 'path': None, 'duration_seconds': None}
    if sound.get('policy') != 'complete_master' or not sound.get('audio_asset_id'):
        raise ValueError('全片音轨设置无效')
    asset_id = str(sound['audio_asset_id'])
    asset = next((item for item in manifest.get('assets', []) if isinstance(item, dict)
                  and item.get('id') == asset_id and item.get('kind') == 'audio'), None)
    if asset is None:
        raise ValueError('全片音轨须选用当前作品已登记的音频素材')
    sources = asset.get('sources') if isinstance(asset.get('sources'), dict) else {}
    selected_source = sources.get('A') or (next(iter(sources.values())) if len(sources) == 1 else None)
    if not selected_source:
        raise ValueError('全片音轨素材须有 A 版本或唯一来源')
    path = resolve_registered_path(str(selected_source))
    if not path.is_file():
        raise ValueError('已登记的全片音轨文件不存在')
    if path.suffix.lower() == '.wav':
        try:
            with wave.open(str(path), 'rb') as audio:
                duration = round(audio.getnframes() / audio.getframerate(), 6)
        except (OSError, wave.Error, ZeroDivisionError) as exc:
            raise ValueError('全片 WAV 音轨无法读取时长') from exc
    else:
        duration = authorized_media_facts(path).get('duration_seconds')
    if duration is None:
        raise ValueError('全片音轨时长无法探测，请使用可读取的 WAV／MP3 母版')
    target = (assembly_trim_summary(manifest, document) or {}).get('playback_seconds')
    if target is None:
        target = sum(float(item.get('duration_seconds') or 0) for item in document.get('segments', []) if isinstance(item, dict))
    if duration + 0.01 < target:
        raise ValueError(f'全片音轨仅 {duration} 秒，短于分镜总时长 {round(target, 3)} 秒；请先用CPU音频处理明确补静音，或在Mac制作完整母版后上传')
    return {'policy': 'complete_master', 'audio_asset_id': asset_id, 'path': str(path),
            'duration_seconds': duration, 'size_bytes': path.stat().st_size,
            'modified_ns': path.stat().st_mtime_ns}


def assembly_edit_view(manifest: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    video_blockers = assembly_video_blockers(document)
    missing_videos = [item['dependency'] for item in video_blockers if item['code'] == 'video_required']
    join_rows = assembly_edit.joins(document, missing_videos=[item['dependency'] for item in video_blockers])
    try:
        sound = assembly_sound_selection(manifest, document)
    except ValueError as exc:
        saved = ((document.get('assembly_edit') or {}).get('sound') or {})
        sound = {'policy': saved.get('policy', 'legacy_master'),
                 'audio_asset_id': saved.get('audio_asset_id'), 'path': None,
                 'duration_seconds': None, 'error': str(exc)}
    warnings = ['非 WAV 完整母版的时长尚未验证，成片后须核对尾部声音'] if (
        sound.get('policy') == 'complete_master' and sound.get('duration_seconds') is None and not sound.get('error')) else []
    return {'project_id': manifest['id'], 'revision': int(document.get('revision', 0) or 0),
            'joins': join_rows, 'blocking_joins': assembly_edit.blocking_joins(document),
            'join_review': assembly_edit.review_readiness(join_rows),
            'missing_videos': missing_videos, 'blockers': video_blockers,
            'sound': sound, 'warnings': warnings, 'target_duration_seconds': round(sum(float(item.get('duration_seconds') or 0)
                       for item in document.get('segments', []) if isinstance(item, dict)), 3),
            'timing_summary': assembly_trim_summary(manifest, document),
            'sound_processing': {'task_path': f"/api/projects/{manifest['id']}/audio-edit-tasks",
                                 'max_seconds': 120, 'gain_range': [0, 8], 'short_audio': 'reject_or_explicit_pad_in_cpu_task',
                                 'long_audio': 'explicit_trim_in_cpu_task', 'mixing': 'complete_master_replaces_all_segment_audio',
                                 'note': '音量和淡入淡出通过原CPU音频处理产生新素材；试听后主动选择母版。原音接缝不自动混合，复杂多轨在Mac剪辑。'},
            'audio_assets': [{'id': item['id'], 'title': item.get('title', item['id'])}
                             for item in manifest.get('assets', []) if isinstance(item, dict)
                             and item.get('kind') == 'audio' and item.get('id')],
            'ready': bool(document.get('segments')) and not video_blockers and
                     not assembly_edit.blocking_joins(document) and not sound.get('error')}


def recalculate_segment_timeline(segments: list[dict[str, Any]]) -> None:
    cursor = 0.0
    for segment in segments:
        duration = round(float(segment.get("duration_seconds", 0) or 0), 3)
        segment["start_seconds"] = round(cursor, 3)
        cursor += duration
        segment["end_seconds"] = round(cursor, 3)


def validate_h3_plan_duration(manifest: dict[str, Any], segment: dict[str, Any]) -> None:
    """Apply the installed shot's H3 contract before a plan edit is saved."""
    stages = [stage for stage in manifest.get('pipeline', []) if isinstance(stage, dict)]
    workflow = segment.get('workflow')
    selected = next((stage for stage in stages if workflow and workflow in
                     ((stage.get('execution') or {}).get('references') or [])), None)
    if selected is None:
        preset = manifest.get('production_preset') or {}
        if preset.get('id') in production_presets.PRESET_IDS:
            selected = next((stage for stage in stages if stage.get('id') ==
                             production_presets.stage_id(preset['id'])), None)
    if selected is None or not str(selected.get('recipe', '')).startswith('H3 '):
        return
    if selected.get('id') not in {production_presets.stage_id(key) for key in production_presets.PRESET_IDS}:
        return
    try:
        h3_duration.plan(segment.get('duration_seconds'))
    except ValueError as exc:
        raise HTTPException(422, f"分镜 {segment.get('id', '')} 时长无效：{exc}") from exc


def persist_plan_edit(project_id: str, document: dict[str, Any], plan_path: Path) -> None:
    recalculate_segment_timeline(document["segments"])
    document["status"] = "draft"
    assembly = document.get("assembly")
    if isinstance(assembly, dict) and assembly.get("output"):
        note = "制作计划已变化，旧成片保留供查看，需要重新装配并审核"
        assembly.update(status="stale", notes=note)
        manifest = load_project_manifest(project_id)
        asset_id = str(manifest.get("assembly_asset_id") or "MASTER")
        with DB_LOCK, db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM project_records WHERE project_id=? AND asset_id=? AND record_type='review:final' ORDER BY revision DESC, id DESC LIMIT 1",
                (project_id, asset_id),
            ).fetchone()
            if row:
                review = json.loads(row["data"])
                if review.get("status") != "stale":
                    append_project_record_in_connection(
                        connection, project_id, "review:final",
                        {**review, "status": "stale", "note": note},
                        asset_id, "plan-editor", time.time(),
                    )
            connection.commit()
    write_plan_version(plan_path, document)


def mark_segment_checkpoints_stale(project_id: str, segment_id: str, note: str) -> None:
    for stage_id in ("scene", "performance", "storyboard", "materials", "generate", "review"):
        append_project_record(
            f"checkpoint:{stage_id}",
            {"stage_id": stage_id, "status": "stale", "note": note, "upstream_revisions": {}},
            asset_id=segment_id,
            source="plan-editor",
            project_id=project_id,
        )
    state = build_project_state(project_id)
    for stage in ("sample", "finish"):
        existing = state.get("reviews", {}).get(segment_id, {}).get(stage)
        if existing:
            append_project_record(
                f"review:{stage}",
                {"stage": stage, "status": "stale", "note": note, "adopted_variant": existing.get("adopted_variant"), "candidate_ref": existing.get("candidate_ref")},
                asset_id=segment_id,
                source="upstream-change",
                project_id=project_id,
            )


@app.delete('/api/projects/{project_id}/plan/segments/{segment_id}')
@plan_transaction
def delete_plan_segment(project_id: str, segment_id: str, request: SegmentDeleteRequest) -> dict[str, Any]:
    if request.confirm != segment_id:
        raise HTTPException(422, '请填写完整分镜编号以确认删除')
    ensure_no_release_blocking_tasks('删除分镜')
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    before = document['segments']
    after = [item for item in before if not isinstance(item, dict) or str(item.get('id')) != segment_id]
    if len(after) == len(before):
        raise HTTPException(404, '分镜不存在')
    for item in after:
        if isinstance(item, dict) and isinstance(item.get('dependencies'), list):
            item['dependencies'] = [value for value in item['dependencies'] if value != segment_id]
    document['segments'] = after
    persist_plan_edit(project_id, document, plan_path)
    return {'deleted_segment_id': segment_id, 'revision': document['revision'], 'plan': document}


@app.patch("/api/projects/{project_id}/plan/segments/{segment_id}")
@plan_transaction
def update_plan_segment(project_id: str, segment_id: str, request: PlanSegmentUpdateRequest) -> dict[str, Any]:
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    segment = next((item for item in document["segments"] if isinstance(item, dict) and item.get("id") == segment_id), None)
    if segment is None:
        raise HTTPException(404, "分镜不存在")
    principal = current_principal.get()
    if principal and not principal.administrator and request.workflow is not None and request.workflow != segment.get('workflow', ''):
        raise HTTPException(403, '工作流由管理员维护，请调整制作方案提供的参数')
    if request.reference_asset_id:
        shot_reference(require_project_manifest(project_id), {'reference_asset_id': request.reference_asset_id})
    updates = request.model_dump(exclude_unset=True)
    if 'generation' in updates:
        if request.generation is None:
            raise HTTPException(422, '请明确选择自动或高级创作，不能清除创作设置。')
        updates['generation'] = ShotGenerationRequest(**{
            **(segment.get('generation') or {}),
            **request.generation.model_dump(exclude_unset=True),
        }).model_dump()
    updates.pop('expected_revision', None)
    updates.pop('expected_script_revision', None)
    updates.pop('script_scene_id', None)
    if request.script_scene_id is not None:
        updates.update(validate_script_link(project_id, request.script_scene_id.strip(), request.expected_script_revision))
    if request.dependencies is not None:
        known_ids = {str(item.get("id")) for item in document["segments"] if isinstance(item, dict)}
        invalid = sorted(set(request.dependencies) - known_ids)
        if segment_id in request.dependencies:
            invalid.append(segment_id)
        if invalid:
            raise HTTPException(422, f"分镜依赖不存在或指向自身: {', '.join(sorted(set(invalid)))}")
    first_frame = updates.pop("first_frame", None) if "first_frame" in updates else None
    last_frame = updates.pop("last_frame", None) if "last_frame" in updates else None
    if request.first_frame is not None or request.last_frame is not None:
        keyframes = dict(segment.get("keyframes") or {})
        if request.first_frame is not None:
            keyframes["first"] = first_frame or None
        if request.last_frame is not None:
            keyframes["last"] = last_frame or None
        segment["keyframes"] = keyframes
    audio_guide = updates.pop("audio_guide", None) if "audio_guide" in updates else None
    delivery_master = updates.pop("delivery_master", None) if "delivery_master" in updates else None
    if request.audio_guide is not None or request.delivery_master is not None:
        audio = dict(segment.get("audio") or {})
        if request.audio_guide is not None:
            audio["guide"] = audio_guide.strip() or None
        if request.delivery_master is not None:
            audio["delivery_master"] = delivery_master.strip() or None
        segment["audio"] = audio
    for key, value in updates.items():
        segment[key] = value.strip() if isinstance(value, str) else value
    if request.workflow and (segment.get('generation') or {}).get('mode') == 'auto':
        raise HTTPException(422, '自动创作不接受技术工作流，请明确切换高级模式。')
    resolve_automatic_shot(require_project_manifest(project_id), segment)
    if request.duration_seconds is not None or request.workflow is not None:
        validate_h3_plan_duration(require_project_manifest(project_id), segment)
    persist_plan_edit(project_id, document, plan_path)
    record_segment_artifacts(project_id, segment)
    mark_segment_checkpoints_stale(project_id, segment_id, "分镜内容已修改，需要重新确认受影响阶段")
    return {"segment": segment, "plan": document, "project": public_project(require_project_manifest(project_id))}


@app.post("/api/projects/{project_id}/plan/segments/{segment_id}/operate")
@plan_transaction
def operate_plan_segment(project_id: str, segment_id: str, request: PlanSegmentOperationRequest) -> dict[str, Any]:
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    segments: list[dict[str, Any]] = document["segments"]
    index = next((offset for offset, item in enumerate(segments) if isinstance(item, dict) and item.get("id") == segment_id), -1)
    if index < 0:
        raise HTTPException(404, "分镜不存在")
    source = segments[index]
    known = {str(item.get("id")) for item in segments}
    affected_ids = {segment_id}

    def unique_id(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in known:
            candidate = f"{base}-{suffix}"
            suffix += 1
        known.add(candidate)
        return candidate

    if request.operation == "copy":
        copied = json.loads(json.dumps(source, ensure_ascii=False))
        copied["id"] = unique_id(f"{segment_id}-copy")
        affected_ids.add(copied["id"])
        copied["status"] = "planned"
        copied.pop("comfyui_task_id", None)
        copied.pop("video", None)
        segments.insert(index + 1, copied)
    elif request.operation == "move":
        if not request.before_id or (request.before_id != "__end__" and request.before_id not in known):
            raise HTTPException(422, "排序操作需要有效的 before_id")
        moving = segments.pop(index)
        if request.before_id == "__end__":
            segments.append(moving)
        else:
            target = next(i for i, item in enumerate(segments) if item.get("id") == request.before_id)
            segments.insert(target, moving)
    elif request.operation == "split":
        split_at = float(request.split_at_seconds or 0)
        duration = float(source.get("duration_seconds", 0) or 0)
        if split_at <= 0 or split_at >= duration:
            raise HTTPException(422, "拆分点必须位于片段时长内部")
        second = json.loads(json.dumps(source, ensure_ascii=False))
        second["id"] = unique_id(f"{segment_id}-B")
        affected_ids.add(second["id"])
        second["duration_seconds"] = round(duration - split_at, 3)
        source["duration_seconds"] = round(split_at, 3)
        for item in (source, second):
            if item.get("video"):
                item.setdefault("version_history", []).append({"video": item["video"], "reason": "分镜拆分前候选"})
            item.pop("video", None)
            item.pop("comfyui_task_id", None)
            item["status"] = "planned"
        segments.insert(index + 1, second)
    elif request.operation in {"merge", "merge_range"}:
        target_id = request.target_id
        target_index = next((i for i, item in enumerate(segments) if item.get("id") == target_id), -1)
        if request.operation == "merge_range" and (request.expected_revision is None or target_index <= index):
            raise HTTPException(422, "范围合并需要计划版本及当前分镜之后的末段")
        if request.operation == "merge" and (target_index < 0 or abs(target_index - index) != 1):
            raise HTTPException(422, "只能合并相邻分镜")
        first_index, second_index = sorted((index, target_index))
        first = segments[first_index]
        merging = segments[first_index:second_index + 1]
        if len({item.get('script_scene_id') or '' for item in merging}) > 1:
            raise HTTPException(409, '不能合并属于不同剧本场次的分镜，请先统一场次关联')
        merged_ids = {item['id'] for item in merging}
        affected_ids.update(merged_ids)
        if first.get('video'):
            first.setdefault('version_history', []).append({
                'video': first['video'], 'comfyui_task_id': first.get('comfyui_task_id'),
                'reason': '分镜合并前候选',
            })
        first['duration_seconds'] = round(sum(float(item.get('duration_seconds', 0)) for item in merging), 3)
        for second in merging[1:]:
            for field in ("location", "shot_size", "camera", "wardrobe", "performance", "prompt"):
                if second.get(field) and second.get(field) != first.get(field):
                    first[field] = " / ".join(value for value in (str(first.get(field, "")).strip(), str(second[field]).strip()) if value)
            first.setdefault("version_history", []).append({"merged_segment": second, "reason": "分镜合并"})
        first['dependencies'] = list(dict.fromkeys(dependency for item in merging
                                    for dependency in (item.get('dependencies') or []) if dependency not in merged_ids))
        first.pop("video", None)
        first.pop("comfyui_task_id", None)
        first["status"] = "planned"
        segments[first_index:second_index + 1] = [first]
        for item in segments:
            if item is first:
                continue
            dependencies = item.get('dependencies') or []
            if any(dependency in merged_ids for dependency in dependencies):
                item['dependencies'] = list(dict.fromkeys(first['id'] if dependency in merged_ids else dependency for dependency in dependencies))
                affected_ids.add(item['id'])
    else:
        raise HTTPException(422, "操作必须是 copy、move、split、merge 或 merge_range")
    if request.operation in {'copy', 'split', 'merge', 'merge_range'}:
        manifest = require_project_manifest(project_id)
        duration_ids = ({copied['id']} if request.operation == 'copy' else
                        {segment_id, second['id']} if request.operation == 'split' else {first['id']})
        for item in segments:
            if item.get('id') in duration_ids:
                validate_h3_plan_duration(manifest, item)
    persist_plan_edit(project_id, document, plan_path)
    for item in segments:
        if item.get("id") in affected_ids:
            record_segment_artifacts(project_id, item)
    for affected_id in sorted(affected_ids):
        mark_segment_checkpoints_stale(project_id, affected_id, f"分镜执行了 {request.operation} 操作")
    return {"plan": document, "segments": segments}


@app.post("/api/projects/{project_id}/upload")
async def upload_project_files(project_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get("workspace_root")
    workspace = (resolve_registered_path(str(workspace_value)) if workspace_value else (DIRECTOR_WORKSPACES_ROOT / project_id).resolve())
    workspace.mkdir(parents=True, exist_ok=True)
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        uploaded = []
        new_media = {}
        original_names = {}
        for upload in files:
            raw_name = (upload.filename or '').replace('\\', '/')
            relative = PurePosixPath(raw_name)
            if not raw_name or relative.is_absolute() or '..' in relative.parts or ':' in raw_name:
                raise HTTPException(422, '非法上传文件名')
            suffix = relative.suffix.lower()
            if suffix not in set().union(*MATERIAL_EXTENSIONS.values()):
                raise HTTPException(422, '不支持此素材格式')
            # Uploaded folders are material batches, never trusted project config.
            destination = PRIVATE_WORKSPACES.file(workspace / 'assets' / 'uploads' / (uuid.uuid4().hex + suffix))
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with destination.open('xb') as handle:
                    while chunk := await upload.read(1024 * 1024):
                        handle.write(chunk)
            finally:
                await upload.close()
            stored = root_relative_path(destination)
            uploaded.append(stored)
            if material_kind(destination) in {'image', 'audio', 'video'}:
                new_media[destination.name] = stored
                original_names[destination.name] = safe_material_label(relative.name) or f'上传素材{suffix}'
        # Re-read under the same lock as other asset registration paths; never
        # import client-supplied manifest/plan/workflow JSON as server state.
        with MATERIAL_REUSE_LOCK:
            manifest = require_project_manifest(project_id)
            manifest['media'] = {**(manifest.get('media') or {}), **new_media}
            additions = imported_asset_records(new_media, original_names)
            manifest['assets'] = [*(manifest.get('assets') or []), *additions]
            save_project_manifest(manifest)
        counts = {kind: sum(asset.get('kind') == kind for asset in additions) for kind in ('image', 'audio', 'video')}
        return {'project': public_project(manifest), 'uploaded': uploaded, 'report': {'counts': counts}}
    uploaded: list[str] = []
    for upload in files:
        raw_name = (upload.filename or "").replace("\\", "/")
        relative = PurePosixPath(raw_name)
        if not raw_name or relative.is_absolute() or ".." in relative.parts:
            raise HTTPException(422, f"非法上传路径: {raw_name}")
        destination = (workspace / Path(*relative.parts)).resolve()
        if workspace != destination and workspace not in destination.parents:
            raise HTTPException(422, f"上传路径越界: {raw_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                handle.write(chunk)
        uploaded.append(root_relative_path(destination))
        await upload.close()
    try:
        refreshed, report = scan_import_directory(workspace, project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    refreshed["series"] = manifest.get("series", refreshed.get("series", "未命名系列"))
    refreshed["title"] = manifest.get("title", refreshed.get("title", project_id))
    for key in ("tag", "eyebrow", "subtitle", "pipeline_contract", "assembly_asset_id", "default_task_asset_id", "pipeline", "task_templates", "assembly"):
        if key in manifest:
            refreshed[key] = manifest[key]
    (workspace / "director.project.json").write_text(json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_project_manifest(refreshed)
    if project_id == CURRENT_PROJECT_ID:
        apply_project_manifest(refreshed)
    return {"project": public_project(refreshed), "uploaded": uploaded, "report": report}


@app.patch('/api/projects/{project_id}/assets/{asset_id}/label')
def update_project_asset_label(project_id: str, asset_id: str, request: AssetLabelRequest) -> dict[str, Any]:
    title = safe_material_label(request.title)
    if not title:
        raise HTTPException(422, '素材名称不能为空或只含不可显示字符')
    with MATERIAL_REUSE_LOCK:
        manifest = require_project_manifest(project_id)
        asset = next((item for item in manifest.get('assets', []) if isinstance(item, dict) and item.get('id') == asset_id), None)
        if asset is None:
            raise HTTPException(404, '素材不存在')
        if asset.get('title') != request.expected_title:
            raise HTTPException(409, '素材名称已变化，请重新读取作品')
        asset['title'] = title
        save_project_manifest(manifest)
        return {'project': public_project(manifest), 'asset': asset}


MATERIAL_EXTENSIONS = {
    "video": {".mp4", ".mov", ".webm", ".mkv", ".m4v"},
    "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "document": {".json", ".txt", ".md", ".srt", ".vtt", ".csv"},
}


def material_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    return next((kind for kind, extensions in MATERIAL_EXTENSIONS.items() if suffix in extensions), "other")


def asset_record_paths(asset: dict[str, Any]) -> list[Any]:
    paths: list[Any] = []
    sources = asset.get("sources")
    if isinstance(sources, dict):
        paths.extend(sources.values())
    if asset.get("image_path"):
        paths.append(asset["image_path"])
    return paths


def windows_downloads_root() -> Path | None:
    configured = os.environ.get("DIRECTOR_DOWNLOADS_ROOT")
    if configured:
        candidate = Path(configured).expanduser()
    else:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            candidate = Path(os.path.expandvars(str(value)))
        except (ImportError, OSError):
            candidate = Path.home() / "Downloads"
    return candidate.resolve() if candidate.is_dir() else None


def material_roots(project_id: str) -> dict[str, tuple[str, Path, str]]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get("workspace_root")
    workspace = resolve_registered_path(str(workspace_value)) if workspace_value else (DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        return {'project': ('当前项目', workspace, 'copy')}
    roots = {
        "input": ("Windows 输入素材", INPUT_ROOT.resolve(), "reference"),
        "output": ("Windows 生成结果", OUTPUT_ROOT.resolve(), "reference"),
        "project": ("当前项目", workspace.resolve(), "reference"),
    }
    downloads = windows_downloads_root()
    if downloads:
        roots["downloads"] = ("Windows 下载目录", downloads, "copy")
    return roots


def resolve_material_path(project_id: str, source_id: str, relative_path: str = "") -> tuple[str, Path, Path, str]:
    roots = material_roots(project_id)
    if source_id not in roots:
        raise HTTPException(422, "未知素材库")
    label, root, import_mode = roots[source_id]
    relative = PurePosixPath(relative_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(422, "非法素材路径")
    candidate = root / Path(*relative.parts)
    target = PRIVATE_WORKSPACES.file(candidate) if PRIVATE_WORKSPACES is not None and current_principal.get() is not None else candidate.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(422, "素材路径越界")
    return label, root, target, import_mode


def public_material_entry(item: Path, root: Path) -> dict[str, Any]:
    relative = item.resolve().relative_to(root).as_posix()
    is_directory = item.is_dir()
    stat = item.stat()
    return {
        "name": item.name,
        "relative_path": relative,
        "entry_type": "directory" if is_directory else "file",
        "kind": "directory" if is_directory else material_kind(item),
        "size_bytes": 0 if is_directory else stat.st_size,
        "modified_at": stat.st_mtime,
        "download_url": None if is_directory else relative,
    }


@app.get("/api/material-library")
def browse_material_library(
    project_id: str,
    source_id: str = "output",
    path: str = "",
    query: str = "",
    kind: str = "all",
) -> dict[str, Any]:
    roots = material_roots(project_id)
    label, root, directory, _ = resolve_material_path(project_id, source_id, path)
    if not directory.is_dir():
        raise HTTPException(404, "素材目录不存在")
    normalized_query = query.strip().casefold()
    if normalized_query and len(normalized_query) < 2:
        raise HTTPException(422, "搜索词至少需要两个字符")
    iterator = root.rglob("*") if normalized_query else directory.iterdir()
    entries: list[dict[str, Any]] = []
    visited = 0
    truncated = False
    for item in iterator:
        visited += 1
        if visited > 5000:
            truncated = True
            break
        if item.name.startswith(".") or item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            continue
        if normalized_query and normalized_query not in item.name.casefold():
            continue
        entry_kind = "directory" if item.is_dir() else material_kind(item)
        if not item.is_dir() and entry_kind == "other":
            continue
        if kind != "all" and not item.is_dir() and entry_kind != kind:
            continue
        try:
            entries.append(public_material_entry(item, root))
        except OSError:
            continue
        if len(entries) >= 200:
            truncated = True
            break
    entries.sort(key=lambda item: (item["entry_type"] != "directory", item["name"].casefold()))
    current_path = "" if normalized_query else directory.relative_to(root).as_posix()
    return {
        "source_id": source_id,
        "source_label": label,
        "current_path": "" if current_path == "." else current_path,
        "query": query.strip(),
        "kind": kind,
        "entries": entries,
        "truncated": truncated,
        "sources": [{"id": item_id, "label": value[0], "import_mode": value[2]} for item_id, value in roots.items()],
    }


@app.get("/material-file")
def download_material_file(project_id: str, source_id: str, path: str) -> FileResponse:
    _, _, target, _ = resolve_material_path(project_id, source_id, path)
    if not target.is_file() or material_kind(target) == "other":
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(target, filename=target.name)


@app.post("/api/projects/{project_id}/materials/register")
def register_server_materials(project_id: str, request: MaterialRegisterRequest) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    media = manifest.get("media") if isinstance(manifest.get("media"), dict) else {}
    assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    workspace_value = manifest.get("workspace_root")
    workspace = resolve_registered_path(str(workspace_value)) if workspace_value else (DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    known_asset_ids = {str(item.get("id")) for item in assets if isinstance(item, dict)}
    registered: list[dict[str, Any]] = []
    for requested in request.items:
        source_label, _, target, import_mode = resolve_material_path(project_id, requested.source_id, requested.relative_path)
        kind = material_kind(target)
        if not target.is_file() or kind not in {"video", "audio", "image"}:
            raise HTTPException(422, f"只能登记图片、音频或视频素材: {requested.relative_path}")
        registered_target = target
        if import_mode == "copy":
            destination_dir = workspace / "assets" / "imported-windows" / requested.source_id
            destination_dir.mkdir(parents=True, exist_ok=True)
            registered_target = destination_dir / target.name
            counter = 2
            while registered_target.exists() and registered_target.stat().st_size != target.stat().st_size:
                registered_target = destination_dir / f"{target.stem}-{counter}{target.suffix}"
                counter += 1
            if not registered_target.exists():
                shutil.copy2(target, registered_target)
            if registered_target.stat().st_size != target.stat().st_size:
                raise HTTPException(500, f"素材导入校验失败: {requested.relative_path}")
        root_relative = root_relative_path(registered_target)
        existing = next(
            (item for item in assets if isinstance(item, dict) and root_relative in asset_record_paths(item)),
            None,
        )
        if existing:
            registered.append(existing)
            continue
        alias_base = f"{requested.source_id}-{target.name}"
        alias = alias_base
        counter = 2
        while alias in media:
            alias = f"{requested.source_id}-{target.stem}-{counter}{target.suffix}"
            counter += 1
        media[alias] = root_relative
        record = imported_asset_records({alias: root_relative})[0]
        base_asset_id = str(record["id"])
        asset_id = base_asset_id
        counter = 2
        while asset_id in known_asset_ids:
            asset_id = f"{base_asset_id}-{counter}"
            counter += 1
        known_asset_ids.add(asset_id)
        action = "已导入项目" if import_mode == "copy" else "工作台引用"
        record.update({"id": asset_id, "origin": f"{source_label} · {action}", "subtitle": "跨设备素材桥登记"})
        assets.append(record)
        registered.append(record)
    manifest["media"] = media
    manifest["assets"] = assets
    save_project_manifest(manifest)
    if project_id == CURRENT_PROJECT_ID:
        apply_project_manifest(manifest)
    return {"project": public_project(manifest), "registered": registered}


@app.get("/api/projects/{project_id}/reusable-materials")
def reusable_materials(project_id: str) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    materials: dict[str, dict[str, Any]] = {}
    diagnostic_paths = set()
    for asset in manifest.get('assets', []):
        if isinstance(asset, dict) and (asset.get('operation') == 'video_qc' or asset.get('purpose') == 'diagnostic'):
            for value in asset_record_paths(asset):
                try:
                    diagnostic_paths.add(str(resolve_registered_path(value)).casefold())
                except (ValueError, OSError):
                    continue

    def add(value: Any, title: str) -> None:
        if not isinstance(value, str) or not value:
            return
        try:
            path = resolve_registered_path(value)
            if str(path).casefold() in diagnostic_paths:
                return
            kind = material_kind(path)
            if not path.is_file() or kind not in {"image", "audio", "video"}:
                return
            stat = path.stat()
        except (ValueError, OSError):
            return
        # A changed source is a new selectable version; never replace a copy
        # that may already be used and reviewed in another work.
        material_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{str(path).casefold()}:{stat.st_size}:{stat.st_mtime_ns}").hex
        materials.setdefault(material_id, {"id": material_id, "title": title, "kind": kind,
                                           "path": str(path), "size_bytes": stat.st_size})

    for asset in manifest.get("assets", []):
        if isinstance(asset, dict):
            for path in asset_record_paths(asset):
                add(path, str(asset.get("title") or asset.get("id") or "项目素材"))
    for title, path in (manifest.get("media") or {}).items():
        add(path, title)
    try:
        plan = json.loads(resolve_registered_path(manifest["plan_path"]).read_text(encoding="utf-8"))
    except (KeyError, ValueError, OSError):
        plan = {}
    for segment in (plan.get("segments", []) if isinstance(plan, dict) else []):
        if not isinstance(segment, dict):
            continue
        label = str(segment.get("id", "分镜"))
        add(segment.get("reference_frame"), f"{label} · 参考画面（非本作首帧）")
        for key, title in (("first", "首帧"), ("last", "尾帧")):
            add((segment.get("keyframes") or {}).get(key), f"{label} · {title}")
        for key, title in (("guide", "表演引导"), ("delivery_master", "交付音频")):
            add((segment.get("audio") or {}).get(key), f"{label} · {title}")
        add((segment.get("video") or {}).get("path"), f"{label} · 候选视频")
    return {"project": {key: manifest.get(key, "") for key in ("id", "title", "series")}, "materials": list(materials.values())}


@app.post("/api/projects/{project_id}/materials/reuse")
def reuse_project_materials(project_id: str, request: MaterialReuseRequest) -> dict[str, Any]:
    with MATERIAL_REUSE_LOCK:
        if project_id == request.source_project_id:
            raise HTTPException(422, "请选择其他作品的素材")
        try:
            manifest = load_project_manifest(project_id)
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc
        available = {item["id"]: item for item in reusable_materials(request.source_project_id)["materials"]}
        requested = list(dict.fromkeys(request.material_ids))
        if any(item not in available for item in requested):
            raise HTTPException(409, "来源素材已变化或不存在，请刷新后重新选择")
        assets = list(manifest.get("assets") or [])
        registered = []
        already_registered = 0
        workspace = resolve_registered_path(str(manifest["workspace_root"])) if manifest.get("workspace_root") else (DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
        for material_id in requested:
            source = available[material_id]
            provenance = {"project_id": request.source_project_id, "material_id": material_id}
            if any(isinstance(asset, dict) and all((asset.get("reused_from") or {}).get(key) == value for key, value in provenance.items()) and _asset_record_exists(asset) for asset in assets):
                already_registered += 1
                continue
            source_path = resolve_registered_path(source["path"])
            destination = (workspace / "assets" / "reused" / uuid.uuid4().hex / source_path.name).resolve()
            if not destination.is_relative_to(workspace.resolve()):
                raise HTTPException(422, "目标素材目录越界")
            try:
                destination.parent.mkdir(parents=True, exist_ok=False)
                before = source_path.stat()
                shutil.copy2(source_path, destination)
                after = source_path.stat()
            except OSError as exc:
                raise HTTPException(409, "素材复制失败，请确认来源文件和目标目录仍可访问后重试") from exc
            if before.st_size != destination.stat().st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise HTTPException(409, "来源素材复制期间发生变化，请刷新后重试")
            current_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{request.source_project_id}:{str(source_path).casefold()}:{after.st_size}:{after.st_mtime_ns}").hex
            if current_id != material_id:
                raise HTTPException(409, "来源素材已变化，请刷新后重新选择")
            relative = root_relative_path(destination)
            record = {"id": f"reused-{uuid.uuid4().hex[:16]}", "kind": source["kind"], "title": source["title"],
                      "ready": True, "origin": "其他作品素材副本", "subtitle": "待本作品审核",
                      "reused_from": {**provenance, "path": source["path"], "copied_at": time.time()}}
            if source["kind"] == "image":
                record["image_path"] = relative
            else:
                record["sources"] = {"A": relative}
            assets.append(record)
            registered.append(record)
        if registered:
            manifest["assets"] = assets
            save_project_manifest(manifest)
            if project_id == CURRENT_PROJECT_ID:
                apply_project_manifest(manifest)
        return {"registered": registered, "already_registered": already_registered}


def reverse_analysis_root(project_id: str) -> Path:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get("workspace_root")
    workspace = resolve_registered_path(str(workspace_value)) if workspace_value else (DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    root = (workspace / "reference-analysis").resolve()
    if workspace != root and workspace not in root.parents:
        raise HTTPException(422, "参考拆解目录越界")
    root.mkdir(parents=True, exist_ok=True)
    return root


def reverse_analysis_path(project_id: str, analysis_id: str) -> Path:
    if not analysis_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in analysis_id):
        raise HTTPException(422, "非法参考拆解 ID")
    root = reverse_analysis_root(project_id)
    target = (root / analysis_id).resolve()
    if root not in target.parents:
        raise HTTPException(422, "参考拆解路径越界")
    return target


def read_reverse_analysis(project_id: str, analysis_id: str) -> tuple[dict[str, Any], Path]:
    path = reverse_analysis_path(project_id, analysis_id) / "analysis.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "参考拆解不存在") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"参考拆解无法读取: {exc}") from exc
    if not isinstance(document, dict) or document.get("project_id") != project_id:
        raise HTTPException(422, "参考拆解与项目不匹配")
    return document, path


def write_reverse_analysis(document: dict[str, Any], path: Path) -> None:
    document["revision"] = int(document.get("revision", 0)) + 1
    document["updated_at"] = time.time()
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class ReferenceStopped(RuntimeError):
    pass


def run_reference_command(command: list[str], *, timeout: float, label: str, task_id: str | None = None) -> subprocess.CompletedProcess[str]:
    """Keep the owned model process tree from outliving a failed analysis request."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=PROJECT, creationflags=flags)
    except OSError as exc:
        raise HTTPException(503, f"{label}无法启动，请检查本机分析工具是否可用") from exc
    try:
        if task_id:
            task = get_task(task_id)
            payload = dict(task['payload'])
            try:
                process_created_at = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess:
                # A short worker may already have finished; its pipe/receipt still determines the result.
                process_created_at = None
            payload['worker_owner'] = {'pid': process.pid, 'created_at': process_created_at}
            set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
            deadline = time.monotonic() + timeout
            while True:
                if (get_task(task_id) or {}).get('stop_requested'):
                    if os.name == 'nt':
                        killed = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, check=False)
                        if killed.returncode and process.poll() is None:
                            raise RuntimeError('Owned reference process tree termination could not be confirmed')
                    elif process.poll() is None:
                        process.kill()
                    process.communicate()
                    raise ReferenceStopped('参考分析已停止；原素材与回执保留')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        else:
            stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            # Only the tree started by this request; never stop another client's model.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, check=False)
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise HTTPException(504, f"{label}超时，已停止本次分析；已保存的素材和人工校订保留，可以重试") from exc
    if process.returncode:
        raise HTTPException(500, f"{label}失败：{stderr.strip()[-2000:] or '分析工具未返回有效结果'}")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_tracked_reference_decomposition(command: list[str], task_id: str | None = None) -> subprocess.CompletedProcess[str]:
    """Whisper ASR in reference decomposition can use CUDA outside ComfyUI."""
    global REFERENCE_DECOMPOSE_ACTIVE
    with GPU_ADMISSION_LOCK:
        REFERENCE_DECOMPOSE_ACTIVE += 1
        GPU_IDLE_RELEASER.note_activity()
    try:
        return run_reference_command(command, timeout=900, label="参考素材拆分", **({'task_id': task_id} if task_id else {}))
    finally:
        with GPU_ADMISSION_LOCK:
            REFERENCE_DECOMPOSE_ACTIVE -= 1


@app.get("/api/models/reverse-analysis")
def reverse_model_status() -> dict[str, Any]:
    return {
        "primary": {
            "id": "qwen3.8-27b-uncensored-q4-k-m",
            "label": "Qwen3.8-27B Uncensored Q4_K_M",
            "architecture": "native_multimodal_dense",
            "role": "vision_story_analysis_prompt_writing",
            "model_ready": PRIMARY_MODEL.is_file(),
            "projector_ready": PRIMARY_MMPROJ.is_file(),
            "ready": PRIMARY_MODEL.is_file() and PRIMARY_MMPROJ.is_file(),
            "model_size_bytes": PRIMARY_MODEL.stat().st_size if PRIMARY_MODEL.is_file() else 0,
            "projector_size_bytes": PRIMARY_MMPROJ.stat().st_size if PRIMARY_MMPROJ.is_file() else 0,
        },
        "fast_optional": {
            "id": "qwen3-vl-8b-instruct-q4-k-m",
            "label": "Qwen3-VL-8B-Instruct Q4_K_M",
            "role": "optional_fast_visual_prepass",
            "ready": FAST_MODEL.is_file() and FAST_MMPROJ.is_file(),
            "model_size_bytes": FAST_MODEL.stat().st_size if FAST_MODEL.is_file() else 0,
        },
    }


@app.get("/api/projects/{project_id}/reverse-analyses")
def list_reverse_analyses(project_id: str) -> dict[str, Any]:
    root = reverse_analysis_root(project_id)
    analyses: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/analysis.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("project_id") == project_id:
            analyses.append(document)
    return {"analyses": analyses}


@app.get("/api/projects/{project_id}/reverse-analyses/{analysis_id}")
def get_reverse_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, _ = read_reverse_analysis(project_id, analysis_id)
    with DB_LOCK, db() as connection:
        row = connection.execute('SELECT id FROM tasks WHERE project_id=? AND asset_id=? ORDER BY created_at DESC LIMIT 1',
                                 (project_id, f'reference-import-{analysis_id}')).fetchone()
    if row:
        task = get_task(row['id'])
        document = {**document, 'import_task_id': task['id'], 'import_status': task['status'],
                    'import_task': {'id': task['id'], 'status': task['status'], 'queue': task['queue'], 'error': task.get('error')}}
    return document


@app.post("/api/projects/{project_id}/reverse-analyses", status_code=202)
async def create_reverse_analysis(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    require_project_manifest(project_id)
    require_submission_capacity()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".flac", ".m4a", ".aac"}:
        raise HTTPException(422, "只支持常见音视频参考素材")
    analysis_id = f"ref-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    output = reverse_analysis_path(project_id, analysis_id)
    source_dir = output / "source"
    source_dir.mkdir(parents=True, exist_ok=False)
    source = source_dir / f"reference{suffix}"
    try:
        with source.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)
    finally:
        await file.close()
    command = [
        sys.executable, str(REFERENCE_DECOMPOSER), "--project-id", project_id,
        "--analysis-id", analysis_id, "--source", str(source), "--output", str(output),
        "--original-name", file.filename or source.name,
    ]
    task_id = str(uuid.uuid4())
    receipt_path = output / 'import-completion.json'
    command.extend(['--task-id', task_id, '--completion-receipt', str(receipt_path)])
    document = {'id': analysis_id, 'project_id': project_id, 'title': file.filename or '参考素材',
                'source': {'original_name': file.filename or source.name, 'path': root_relative_path(source), 'duration_seconds': 0, 'width': None, 'height': None},
                'stages': [{'id': 'materials', 'status': 'waiting'}], 'timeline': [], 'artifacts': {},
                'import_task_id': task_id, 'import_status': 'scheduler_waiting'}
    payload = {'kind': 'reference_decomposition', 'project_id': project_id, 'asset_id': f'reference-import-{analysis_id}',
               'analysis_id': analysis_id, 'source': str(source), 'analysis_path': str(output / 'analysis.json'), 'receipt_path': str(receipt_path), 'command': command}
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    with GPU_ADMISSION_LOCK, DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                           (task_id, payload['asset_id'], 'scheduler_waiting', time.time(), time.time(), json.dumps(payload, ensure_ascii=False), project_id))
        write_reverse_analysis(document, output / 'analysis.json')
        connection.commit()
        GPU_IDLE_RELEASER.note_activity()
    TASK_SCHEDULER.wake()
    return get_reverse_analysis(project_id, analysis_id)


@app.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/artifacts/{artifact_id}")
def update_reverse_artifact(project_id: str, analysis_id: str, artifact_id: str, request: ReverseArtifactUpdateRequest) -> dict[str, Any]:
    with REVERSE_DOCUMENT_LOCK:
        document, path = read_reverse_analysis(project_id, analysis_id)
        current_revision = int(document.get("revision", 0))
        if request.expected_revision is not None and request.expected_revision != current_revision:
            raise HTTPException(409, {"code": "analysis_revision_conflict", "message": "参考分析已更新，请比较最新内容后再保存；本次校订未覆盖服务器", "current_revision": current_revision, "expected_revision": request.expected_revision, "retryable": False})
        artifacts = document.get("artifacts") if isinstance(document.get("artifacts"), dict) else {}
        artifact = artifacts.get(artifact_id)
        if not isinstance(artifact, dict):
            raise HTTPException(404, "拆解工件不存在")
        if "original_candidate" not in artifact:
            artifact["original_candidate"] = {key: value for key, value in artifact.items() if key not in {"original_candidate", "model_candidate", "review_history"}}
        previous = {key: artifact.get(key) for key in ("summary", "user_status", "status", "provenance", "confidence")}
        previous.update(revision=current_revision, recorded_at=time.time())
        artifact.setdefault("review_history", []).append(previous)
        artifact["summary"] = request.summary.strip()
        artifact["user_status"] = request.user_status
        if request.user_status == "reviewed":
            artifact["provenance"] = "user_confirmed"
            artifact["status"] = "ready"
        if request.confidence is not None:
            artifact["confidence"] = request.confidence
        write_reverse_analysis(document, path)
        return {"artifact_id": artifact_id, "artifact": artifact, "analysis": document}


@app.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/classification")
def correct_reverse_classification(project_id: str, analysis_id: str, request: ReverseClassificationRequest) -> dict[str, Any]:
    reason = request.reason.strip()
    if not reason:
        raise HTTPException(422, '请填写内容类型校订依据')
    with REVERSE_DOCUMENT_LOCK:
        document, path = read_reverse_analysis(project_id, analysis_id)
        current_revision = int(document.get('revision', 0))
        if request.expected_revision != current_revision:
            raise HTTPException(409, {'code': 'analysis_revision_conflict', 'message': '参考分析已更新，请读取后再校订内容类型',
                                     'current_revision': current_revision, 'expected_revision': request.expected_revision, 'retryable': False})
        previous = {'content_mode': document.get('content_mode'), 'reason': document.get('content_mode_reason', ''),
                    'user_status': document.get('content_mode_user_status', 'unreviewed'), 'revision': current_revision}
        document.setdefault('content_mode_history', []).append(previous)
        document.setdefault('content_mode_original_candidate', previous.copy())
        document['content_mode'] = request.content_mode
        document['content_mode_reason'] = reason
        document['content_mode_user_status'] = 'reviewed'
        write_reverse_analysis(document, path)
        return {'analysis': document}


@app.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/timeline/{segment_id}")
def update_reverse_timeline(project_id: str, analysis_id: str, segment_id: str, request: ReverseTimelineUpdateRequest) -> dict[str, Any]:
    with REVERSE_DOCUMENT_LOCK:
        document, path = read_reverse_analysis(project_id, analysis_id)
        current_revision = int(document.get("revision", 0))
        if request.expected_revision is not None and request.expected_revision != current_revision:
            raise HTTPException(409, {"code": "analysis_revision_conflict", "message": "参考分析已更新，请比较最新内容后再保存；本次校订未覆盖服务器", "current_revision": current_revision, "expected_revision": request.expected_revision, "retryable": False})
        timeline = document.get("timeline") if isinstance(document.get("timeline"), list) else []
        segment = next((item for item in timeline if isinstance(item, dict) and item.get("id") == segment_id), None)
        if not segment:
            raise HTTPException(404, "参考时间段不存在")
        segment["user_note"] = request.user_note.strip()
        segment["status"] = request.status
        write_reverse_analysis(document, path)
        return {"segment": segment, "analysis": document}


@app.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic", status_code=202)
def run_reverse_semantic_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    return start_semantic_job(project_id, analysis_id)


def reserve_reverse_semantic(project_id: str, analysis_id: str) -> Path:
    _, path = read_reverse_analysis(project_id, analysis_id)
    if not PRIMARY_MODEL.is_file() or not PRIMARY_MMPROJ.is_file():
        raise HTTPException(409, "27B 多模态主模型或视觉投影尚未就绪")
    with GPU_ADMISSION_LOCK:
        ensure_no_unresolved_analysis_jobs()
        if not REVERSE_SEMANTIC_LOCK.acquire(blocking=False):
            raise HTTPException(409, "已有参考内容分析正在运行，请等待完成后再试")
        try:
            ensure_no_release_blocking_tasks("开始参考内容分析", gpu_only=True)
            info = comfy_status()
            if info.get("queue_running", 0) or info.get("queue_pending", 0):
                raise HTTPException(409, "ComfyUI 正在处理任务，请等待完成后再分析参考")
            GPU_IDLE_RELEASER.note_activity()
        except Exception:
            REVERSE_SEMANTIC_LOCK.release()
            raise
    return path


def merge_semantic_result(project_id: str, analysis_id: str, result_path: Path) -> dict[str, Any]:
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("artifacts"), dict):
            raise ValueError("missing artifacts")
    except (OSError, ValueError) as exc:
        raise HTTPException(502, "内容分析未产生有效结果，已有校订保持不变") from exc
    from tools.reference_semantic import apply_result
    with REVERSE_DOCUMENT_LOCK:
        document, path = read_reverse_analysis(project_id, analysis_id)
        if document.get("semantic_result") != root_relative_path(result_path):
            apply_result(document, result)
            document["semantic_result"] = root_relative_path(result_path)
            write_reverse_analysis(document, path)
    return document


def execute_reverse_semantic(project_id: str, analysis_id: str, path: Path, job: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if PRIVATE_WORKSPACES is not None and job is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': project_id, 'payload': job})
        result_path = Path(job["result_path"]) if job else path.parent / "semantic-results" / f"{uuid.uuid4().hex}.json"
        receipt_args = ["--completion-receipt", job["receipt_path"], "--job-id", job["id"]] if job else []
        # A full 36-frame analysis may require nine visual batches plus synthesis.
        run_reference_command(
            [sys.executable, str(REFERENCE_SEMANTIC_ANALYZER), "--analysis", str(path), "--result-output", str(result_path), *receipt_args],
            timeout=10800, label="参考内容分析", **({'task_id': job['task_id']} if job and job.get('task_id') else {}),
        )
        if job and (get_task(job.get('task_id', '')) or {}).get('stop_requested'):
            return read_reverse_analysis(project_id, analysis_id)[0]
        return merge_semantic_result(project_id, analysis_id, result_path)
    finally:
        REVERSE_SEMANTIC_LOCK.release()


@app.get("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs/current")
def current_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, path = read_reverse_analysis(project_id, analysis_id)
    with background_jobs.LOCK:
        job = next((job for job in background_jobs.records(analysis_job_root()) if job["project_id"] == project_id and job["analysis_id"] == analysis_id), None)
    public_job = background_jobs.public(job)
    if job and job.get('task_id'):
        task = get_task(job['task_id'])
        if task:
            public_job['status'] = 'queued' if task['status'] in {'scheduler_waiting', 'batch_waiting'} else task['status']
            public_job['queue'] = TASK_SCHEDULER.queue_info(task['id'])
            public_job['error'] = task.get('error')
    if public_job and public_job['status'] in {'queued', 'running'}:
        # Report only persisted observations belonging to this exact source.
        # Frame completion is not completion of the subsequent synthesis.
        from tools.reference_semantic import pick_frames, checkpoint_signature, load_observation_checkpoint
        try:
            frames = pick_frames(document)
            checkpoint = path.with_name('semantic-observations.json')
            signature = checkpoint_signature(document, frames)
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            if frames and isinstance(saved, dict) and saved.get('signature') == signature:
                observations = load_observation_checkpoint(checkpoint, signature, [item[0] for item in frames])
                public_job['progress'] = {'observed_frames': len(observations), 'total_frames': len(frames)}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass  # A missing or incomplete checkpoint must not hide the job.
    return {"job": public_job}


@app.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs", status_code=202)
def start_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, path = read_reverse_analysis(project_id, analysis_id)
    root = analysis_job_root()
    with GPU_ADMISSION_LOCK, background_jobs.LOCK:
        previous = next((job for job in background_jobs.records(root) if job["project_id"] == project_id and job["analysis_id"] == analysis_id and job["status"] in background_jobs.ACTIVE), None)
        if previous and previous.get('task_id'):
            task = get_task(previous['task_id'])
            if task and task['status'] in {'succeeded', 'failed', 'stopped'}:
                previous = None
        if previous:
            return current_semantic_job(project_id, analysis_id)
        if document.get('import_task_id') and (get_task(document['import_task_id']) or {}).get('status') != 'succeeded':
            raise HTTPException(422, {'code': 'reference_import_not_ready', 'message': '请先等待参考素材拆分完成，再开始内容分析', 'action': 'inspect_task'})
        if not PRIMARY_MODEL.is_file() or not PRIMARY_MMPROJ.is_file():
            raise HTTPException(409, {'code': 'model_unavailable', 'message': '参考语义分析模型尚未就绪'})
        require_submission_capacity()
        now = time.time()
        with DB_LOCK, db() as connection:
            check_queue_limits(connection, settings_owner())
            job = background_jobs.create(root, project_id, analysis_id)
            if PRIVATE_WORKSPACES is not None:
                PRIVATE_WORKSPACES.freeze_owner(job, project_id)
            result_path = path.parent / "semantic-results" / f"{job['id']}.json"
            frozen_input = path.with_name(f"semantic-input-{job['id']}.json")
            frozen_input.write_bytes(path.read_bytes())
            job = background_jobs.update(root, job, result_path=str(result_path), receipt_path=str(result_path.with_suffix('.receipt.json')),
                                         task_id=job['id'], analysis_path=str(frozen_input), source_revision=document.get('revision', 0))
            payload = {**job, 'kind': 'reference_semantic', 'analysis_path': str(frozen_input), 'asset_id': f'reference-{analysis_id}',
                       'scheduler_dependencies': [document['import_task_id']] if document.get('import_task_id') else []}
            connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                               (job['id'], payload['asset_id'], 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        TASK_SCHEDULER.wake()
        return current_semantic_job(project_id, analysis_id)


@app.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs/reconcile")
def reconcile_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    _, analysis_path = read_reverse_analysis(project_id, analysis_id)
    with GPU_ADMISSION_LOCK, background_jobs.LOCK:
        job = next((item for item in background_jobs.records(analysis_job_root()) if item['project_id'] == project_id and item['analysis_id'] == analysis_id), None)
        if not job:
            raise HTTPException(404, '没有可核对的分析任务')
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': project_id, 'payload': job})
        if job['status'] == 'succeeded':
            return {'job': background_jobs.public(job)}
        if job['status'] != 'needs_reconcile':
            raise HTTPException(409, '当前任务不需要重启恢复核对')
        result_path = analysis_path.parent / 'semantic-results' / f"{job['id']}.json"
        receipt_path = result_path.with_suffix('.receipt.json')
        if job.get('result_path') != str(result_path) or job.get('receipt_path') != str(receipt_path):
            raise HTTPException(409, '旧任务缺少独立完成回执，无法确认原分析是否结束')
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise HTTPException(409, '尚未发现完整完成回执，原分析可能仍在运行，请稍后再次核对') from exc
        input_path = Path(job['analysis_path']) if job.get('task_id') and job.get('analysis_path') else analysis_path
        expected = {'job_id': job['id'], 'analysis_path': str(input_path.resolve()), 'result_path': str(result_path.resolve()), 'status': 'succeeded'}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise HTTPException(409, '完成回执与原任务不匹配，保留待核对状态')
        merge_semantic_result(project_id, analysis_id, result_path)
        job = background_jobs.update(analysis_job_root(), job, status='succeeded', error=None, recovered_at=time.time())
        if job.get('task_id'):
            set_task(job['task_id'], status='succeeded', error=None)
            TASK_SCHEDULER.wake()
        return {'job': background_jobs.public(job)}


@app.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/convert")
@plan_transaction
def convert_reverse_analysis(project_id: str, analysis_id: str, request: ReverseConvertRequest | None = None) -> dict[str, Any]:
    with REVERSE_DOCUMENT_LOCK:
        if request is not None:
            document, _ = read_reverse_analysis(project_id, analysis_id)
            current_revision = int(document.get('revision', 0))
            if request.expected_revision != current_revision:
                raise HTTPException(409, {'code': 'analysis_revision_conflict', 'message': '参考拆解已更新，请重新读取并核对后再转为制作草案', 'current_revision': current_revision, 'retryable': False})
        return _convert_reverse_analysis(project_id, analysis_id)


def _convert_reverse_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, analysis_path = read_reverse_analysis(project_id, analysis_id)
    if not document.get("timeline"):
        raise HTTPException(422, "参考拆解尚无时间段，请先完成拆解")
    manifest = load_project_manifest(project_id)
    workspace = resolve_registered_path(str(manifest.get("workspace_root")))
    if not manifest.get("plan_path"):
        plan_path = workspace / "plans" / "plan.json"
        manifest["plan_path"] = root_relative_path(plan_path)
        save_project_manifest(manifest)
        if project_id == CURRENT_PROJECT_ID:
            apply_project_manifest(manifest)
    plan, plan_path = load_project_plan(project_id)
    segments = plan.get("segments") if isinstance(plan.get("segments"), list) else []
    known = {str(item.get("id")) for item in segments if isinstance(item, dict)}
    # Merged references remain consumed. Older plans already keep their complete
    # source records in version_history, so no migration or duplicate index is needed.
    linked: set[str] = set()
    pending = list(segments)
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        if item.get("source_analysis") == analysis_id:
            linked.add(str(item.get("source_segment")))
        for entry in item.get("version_history") or []:
            if isinstance(entry, dict) and isinstance(entry.get("merged_segment"), dict):
                pending.append(entry["merged_segment"])
    created: list[dict[str, Any]] = []
    for index, reference in enumerate(document.get("timeline", []), start=1):
        if not isinstance(reference, dict):
            continue
        if str(reference.get("id")) in linked:
            continue
        base_id = f"REF-{analysis_id[-8:]}-{index:02d}"
        segment_id = base_id
        counter = 2
        while segment_id in known:
            segment_id = f"{base_id}-{counter}"
            counter += 1
        known.add(segment_id)
        duration = max(float(reference.get("end_seconds", 0)) - float(reference.get("start_seconds", 0)), 0.1)
        note = str(reference.get("user_note") or reference.get("inference") or "等待人工补充分镜意图")
        segment = {
            "id": segment_id,
            "duration_seconds": round(duration, 3),
            "status": "planned",
            "location": "由参考拆解待确认",
            "shot_size": "待识别",
            "camera": "待识别",
            "wardrobe": "参考机制，不直接复制具体造型",
            "performance": note,
            "prompt": None,
            "workflow": None,
            "keyframes": {"first": None, "last": None},
            "reference_frame": reference.get("frame"),
            "source_analysis": analysis_id,
            "source_segment": reference.get("id"),
            "source_revision": int(document.get("revision", 0)),
            "source_range_seconds": [reference.get("start_seconds", 0), reference.get("end_seconds", 0)],
            "source_note_status": reference.get("status", "pending_review"),
            "source_observation": str(reference.get("inference") or ""),
            "source_user_note": str(reference.get("user_note") or ""),
        }
        created.append(segment)
        segments.append(segment)
        linked.add(str(reference.get("id")))
    plan["segments"] = segments
    if created:
        persist_plan_edit(project_id, plan, plan_path)
    for segment in created:
        record_segment_artifacts(project_id, segment, "reverse-analysis")
    seed = {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "project_id": project_id,
        "created_segments": [item["id"] for item in segments if isinstance(item, dict) and item.get("source_analysis") == analysis_id],
        "artifacts": document.get("artifacts", {}),
        "note": "参考拆解仅生成制作草案；角色、场景、动作与传播机制仍需人工确认。",
    }
    saved_seed = document.get("production_seed")
    seed_path = resolve_registered_path(saved_seed) if saved_seed else analysis_path.parent / "production-seed.json"
    if created and seed_path.exists():
        seed_path = analysis_path.parent / f"production-seed-{time.time_ns()}.json"
    if created or not seed_path.exists():
        seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    document["status"] = "converted_to_production"
    for stage in document.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == "production":
            stage["status"] = "done"
    document["production_seed"] = root_relative_path(seed_path)
    write_reverse_analysis(document, analysis_path)
    return {"analysis": document, "created_segments": created, "plan": plan, "production_seed": root_relative_path(seed_path)}


@app.post("/api/projects/import")
def import_project(request: ProjectImportRequest) -> dict[str, Any]:
    ensure_no_release_blocking_tasks("导入项目")
    raw_path = Path(request.path)
    source = raw_path if raw_path.is_absolute() else ROOT / raw_path
    try:
        source = source.resolve()
        source.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(403, "只能导入 D:\\Comfy-Desktop 工作区内的目录") from exc
    if not source.is_dir():
        raise HTTPException(404, "导入目录不存在")
    project_id = request.project_id or source.name
    project_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in project_id).strip("-")[:80] or "imported-project"
    catalog = load_project_catalog()
    existing = {str(item.get("id")) for item in catalog.get("projects", []) if isinstance(item, dict)}
    if project_id in existing:
        suffix = 2
        base = project_id
        while f"{base}-{suffix}" in existing:
            suffix += 1
        project_id = f"{base}-{suffix}"
    try:
        manifest, report = scan_import_directory(source, project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        if manifest.get("plan_path"):
            resolve_registered_path(str(manifest["plan_path"]))
        for registered in (manifest.get("media") or {}).values():
            resolve_registered_path(str(registered))
        assembly = manifest.get("assembly") or {}
        if assembly.get("audio"):
            resolve_registered_path(str(assembly["audio"]))
    except ValueError as exc:
        raise HTTPException(422, f"项目清单包含工作区外路径: {exc}") from exc
    manifest_path = PROJECTS_ROOT / f"{project_id}.json"
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog.setdefault("projects", []).append({"id": project_id, "manifest": manifest_path.name})
    catalog["default_project_id"] = project_id
    PROJECT_CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    apply_project_manifest(manifest)
    return {"project": public_project(), "report": report, "manifest_path": root_relative_path(manifest_path)}


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> dict[str, Any]:
    try:
        manifest = load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    value = public_project(manifest)
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    files: list[str] = []
    for path in media.values():
        try:
            if resolve_registered_path(str(path)).is_file():
                files.append(str(path).replace("\\", "/"))
        except ValueError:
            continue

    # A project export is the review/reproducibility handoff, so it must carry
    # the plan and the exact ComfyUI graphs referenced by that plan. The media
    # files remain in ComfyUI-Shared; this JSON records portable paths plus the
    # graph contents needed to reopen or audit the work later.
    plan_document: dict[str, Any] | None = None
    plan_path = manifest.get("plan_path")
    if plan_path:
        try:
            resolved_plan = resolve_registered_path(str(plan_path))
            if resolved_plan.is_file():
                loaded_plan = json.loads(resolved_plan.read_text(encoding="utf-8-sig"))
                if isinstance(loaded_plan, dict):
                    plan_document = loaded_plan
        except (ValueError, OSError, json.JSONDecodeError):
            plan_document = None

    workflow_refs: list[tuple[str, str]] = []
    if plan_document:
        for segment in plan_document.get("segments", []):
            if not isinstance(segment, dict):
                continue
            if segment.get("workflow"):
                workflow_refs.append(("segment", str(segment["workflow"])))
            recipes = segment.get("keyframe_recipes")
            if isinstance(recipes, dict):
                for role, recipe in recipes.items():
                    if recipe:
                        workflow_refs.append((f"keyframe:{role}", str(recipe)))
        assembly = plan_document.get("assembly")
        if isinstance(assembly, dict) and assembly.get("workflow"):
            workflow_refs.append(("assembly", str(assembly["workflow"])))
    catalog_workflows = manifest.get("workflow_catalog")
    if isinstance(catalog_workflows, list):
        for item in catalog_workflows:
            if isinstance(item, dict) and item.get("path"):
                role = str(item.get("role") or "catalog")
                asset_id = str(item.get("asset_id") or "")
                workflow_refs.append((f"catalog:{role}:{asset_id}" if asset_id else f"catalog:{role}", str(item["path"])))
    configured_assembly = manifest.get("assembly")
    if isinstance(configured_assembly, dict) and configured_assembly.get("builder"):
        workflow_refs.append(("assembly-builder", str(configured_assembly["builder"])))

    workflow_files: list[dict[str, Any]] = []
    seen_workflows: set[str] = set()
    for role, reference in workflow_refs:
        normalized = reference.replace("\\", "/")
        if normalized in seen_workflows:
            continue
        seen_workflows.add(normalized)
        record: dict[str, Any] = {"role": role, "path": normalized, "exists": False}
        try:
            try:
                workflow_path = resolve_registered_path(reference)
            except ValueError:
                # Manifest helper references are project-relative (for
                # example tools/build_assembly_workflow.py), while media and
                # segment recipes may be shared-root-relative or absolute.
                candidate = Path(reference)
                workflow_path = (PROJECT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                if not (workflow_path == PROJECT.resolve() or PROJECT.resolve() in workflow_path.parents):
                    raise
            record["path"] = root_relative_path(workflow_path)
            record["exists"] = workflow_path.is_file()
            if workflow_path.is_file():
                record["size_bytes"] = workflow_path.stat().st_size
                try:
                    record["content"] = json.loads(workflow_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    record["content"] = workflow_path.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            record["error"] = "路径不在允许的 ComfyUI 工作区内或文件不存在"
        workflow_files.append(record)

    task_records: list[dict[str, Any]] = []
    planned_asset_ids = {str(item.get("id")) for item in (plan_document or {}).get("segments", []) if isinstance(item, dict) and item.get("id")}
    assembly_asset_id = str(manifest.get("assembly_asset_id", ""))
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
        if planned_asset_ids or assembly_asset_id:
            fallback_rows = connection.execute("SELECT * FROM tasks WHERE project_id IS NULL AND asset_id IN ({}) ORDER BY created_at".format(",".join("?" for _ in (planned_asset_ids | {assembly_asset_id}))), tuple(planned_asset_ids | {assembly_asset_id})).fetchall()
            rows = list(rows) + [row for row in fallback_rows if row["id"] not in {item["id"] for item in rows}]
    task_records = [task_row(row) for row in rows]

    return {
        "schema_version": 2,
        "export_type": "director-workbench-project-bundle",
        "project": value,
        "plan": {"path": str(plan_path).replace("\\", "/") if plan_path else None, "content": plan_document},
        "workflow_files": workflow_files,
        "task_records": task_records,
        "project_state": build_project_state(project_id),
        "resource_manifest": {"files": files, "count": len(files)},
        "workspace_spec": "docs/workspace-spec.md",
    }


def validate_pipeline_values(stage: dict[str, Any], values: dict[str, Any], manifest: dict[str, Any] | None = None, *, collect_errors: bool = False) -> dict[str, Any]:
    contract = pipeline_contract(stage, manifest)
    specs = {item["id"]: item for item in contract["input_specs"]}
    unknown = sorted(set(values) - set(specs))
    blockers = [{'code': 'unknown_input', 'message': '制作步骤不接受此参数', 'input_id': key, 'action': 'edit_inputs'} for key in unknown]
    normalized: dict[str, Any] = {}
    args: list[str] = []
    for key, spec in specs.items():
        present = key in values
        value = values.get(key)
        if not present or value in (None, "", []):
            if spec.get("required") and not spec.get("allowEmpty"):
                blockers.append({'code': 'missing_input', 'message': f"缺少必填输入: {spec['label']}", 'input_id': key, 'action': 'upload_material' if spec.get('kind') in {'图片', '音频', '视频'} else 'edit_inputs'})
            continue
        control = spec.get("control", "freeform")
        if control == "select" and value not in spec.get("options", []):
            blockers.append({'code': 'invalid_option', 'message': f"{spec['label']} 只能选择: {', '.join(spec.get('options', []))}", 'input_id': key, 'action': 'edit_inputs'})
            continue
        if control == "boolean" and not isinstance(value, bool):
            options = spec.get("options", ["enable", "disable"])
            if value not in options:
                blockers.append({'code': 'invalid_option', 'message': f"{spec['label']} 只能选择: {', '.join(options)}", 'input_id': key, 'action': 'edit_inputs'})
                continue
        if control == "multi-select":
            if not isinstance(value, list) or any(item not in spec.get("options", []) for item in value):
                blockers.append({'code': 'invalid_option', 'message': f"{spec['label']} 包含未允许的选项", 'input_id': key, 'action': 'edit_inputs'})
                continue
        if control == "number":
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = float('nan')
            if isinstance(value, bool) or not math.isfinite(numeric) or (spec.get("min") is not None and numeric < spec["min"]) or (spec.get("max") is not None and numeric > spec["max"]):
                blockers.append({'code': 'invalid_number', 'message': f"{spec['label']} 必须是允许范围内的有限数字", 'input_id': key, 'action': 'edit_inputs', 'min': spec.get('min'), 'max': spec.get('max')})
                continue
            value = int(numeric) if numeric.is_integer() else numeric
        normalized[key] = value
        flag = str(spec.get("flag", "")).strip()
        if flag:
            if spec.get("flagMode") == "presence":
                enabled = value is True or value == "enable"
                if enabled:
                    args.append(flag)
            elif isinstance(value, list):
                args.extend(item for selected in value for item in (flag, str(selected)))
            else:
                args.extend((flag, str(value)))
        elif spec.get("serialize") == "raw-args":
            try:
                args.extend(shlex.split(str(value), posix=False))
            except ValueError as exc:
                blockers.append({'code': 'invalid_arguments', 'message': f"{spec['label']} 的参数串引号不完整", 'input_id': key, 'action': 'edit_inputs'})
    if blockers and not collect_errors:
        raise HTTPException(422, {'code': 'invalid_inputs', 'message': '请补齐或修正制作输入', 'blockers': blockers})
    result = {"stage_id": stage["id"], "valid": not blockers, "values": normalized, "args": args,
              "command_preview": " ".join(shlex.quote(item) for item in args)}
    if collect_errors:
        result['blockers'] = blockers
    return result


def mark_orphans() -> None:
    with DB_LOCK, db() as connection:
        connection.execute("UPDATE tasks SET status = 'needs_reconcile', error = '服务重启后需与 ComfyUI 历史核对', updated_at = ? WHERE status IN ('submitting','queued','running','stopping')", (time.time(),))
        rows = connection.execute("SELECT id,payload FROM tasks WHERE status IN ('preparing','submitting','queued','running','stopping','stop_requested','needs_reconcile')").fetchall()
        for row in rows:
            if json.loads(row['payload']).get('kind') in {'audio_edit', 'media_operation'}:
                connection.execute("UPDATE tasks SET status='needs_reconcile',error='服务重启后需核对CPU媒体处理回执；不会自动重执行',updated_at=? WHERE id=?", (time.time(), row['id']))
        connection.commit()


@app.on_event("startup")
def start_workbench() -> None:
    mark_orphans()
    TASK_SCHEDULER.start()
    GPU_IDLE_RELEASER.start()


@app.on_event("shutdown")
def stop_gpu_idle_release() -> None:
    TASK_SCHEDULER.stop()
    GPU_IDLE_RELEASER.stop()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "director-workbench", "comfy": comfy_status()}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {"comfy": comfy_status(), "runtime": str(RUNTIME)}


@app.get("/api/plan")
def plan() -> dict[str, Any]:
    """Return the latest generation plan without claiming it is a final cut."""
    if not PLAN_PATH.is_file():
        return {"schema_version": 1, "project": CURRENT_PROJECT_ID, "status": "empty", "segments": [], "assembly": None}
    try:
        return json.loads(PLAN_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"无法读取作品分段计划: {exc}") from exc


@app.get("/api/pipeline")
def pipeline() -> dict[str, Any]:
    """Return the operator-facing stage contracts for the current work."""
    return {"schema_version": 2, "project": CURRENT_PROJECT_ID, "stages": [pipeline_contract(stage) for stage in PIPELINE_STAGES]}


def pipeline_value_kind(label: str) -> str:
    """Give the UI a stable, human-facing type without exposing node types."""
    value = label.lower()
    if any(token in value for token in ("wav", "音频", "母版", "干声", "伴奏", "声线", "音色")):
        return "音频"
    if any(token in value for token in ("png", "图片", "首帧", "尾帧", "角色参考", "场景参考")):
        return "图片"
    if any(token in value for token in ("mp4", "视频", "片段")):
        return "视频"
    if any(token in value for token in ("提示词", "歌词")):
        return "文本"
    if any(token in value for token in ("时间", "区间", "顺序", "转场", "seed", "时长", "画幅")):
        return "参数"
    return "参数"


def pipeline_control(label: str) -> dict[str, Any]:
    """Describe the safest editor for a legacy string input.

    New steps can provide richer metadata directly; this fallback keeps older
    stage rows usable while still preventing obvious enum inputs from becoming
    arbitrary text fields.
    """
    value = label.lower()
    if "enable" in value or "disable" in value:
        return {"control": "select", "options": ["enable", "disable"]}
    if "seed" in value or "时长" in value:
        return {"control": "number", "min": 0, "step": 1}
    if "提示词" in label or "歌词" in label:
        return {"control": "text"}
    if any(token in label for token in ("音频", "母版", "首帧", "尾帧", "参考", "视频", ".wav", ".mp4", ".png")):
        return {"control": "asset"}
    return {"control": "freeform", "allowEmpty": True}


def pipeline_contract(stage: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize legacy stage rows into the input/output contract consumed by the UI.

    The backend intentionally keeps this as metadata. It does not execute a
    script or a ComfyUI graph merely because a stage is displayed.
    """
    value = dict(stage)
    # Re-read the small contract file on request so adding a flag is a quick
    # JSON edit followed by a page refresh; the imported map remains the
    # fallback for a temporarily unreadable file.
    if manifest is None:
        declared = load_pipeline_input_contracts().get(stage["id"]) or PIPELINE_INPUT_CONTRACTS.get(stage["id"])
    else:
        contract_path = (PROJECT / str(manifest.get('pipeline_contract', 'contracts/pipeline-inputs.json'))).resolve()
        if not contract_path.is_relative_to(PROJECT.resolve()):
            raise HTTPException(422, '制作步骤契约路径越界')
        declared = load_pipeline_input_contracts(contract_path).get(stage['id'])
    if isinstance(stage.get("input_specs"), list):
        value["input_specs"] = stage["input_specs"]
    elif declared and declared.get("mode") == "replace":
        value["input_specs"] = declared["inputs"]
    else:
        value["input_specs"] = [
            {"id": f"input-{index}", "label": label, "kind": pipeline_value_kind(label),
             "required": True, "description": f"提交“{stage['title']}”前需要准备的输入.", **pipeline_control(label)}
            for index, label in enumerate(stage.get("inputs", []), start=1)
        ] + PIPELINE_INPUT_OVERRIDES.get(stage["id"], [])
    # Existing installed recipes keep their saved graph and shots. Only the
    # advertised contract is refreshed when the server gains duration support.
    preset_id = next((key for key in production_presets.PRESET_IDS
                      if stage.get('id') == production_presets.stage_id(key)
                      and stage.get('recipe', '').startswith('H3 ')), None)
    if preset_id:
        value['input_specs'] = production_presets.input_specs(preset_id)
    output_state = "待接入" if stage.get("status") == "待接入" else "待生成"
    value["output_specs"] = stage["output_specs"] if isinstance(stage.get("output_specs"), list) else [
        {"id": f"output-{index}", "label": label, "kind": pipeline_value_kind(label),
         "description": f"“{stage['title']}”完成后登记的结果。", "state": output_state}
        for index, label in enumerate(stage.get("outputs", []), start=1)
    ]
    mode = "comfyui" if stage.get("backend") == "ComfyUI 工作流" else "script" if stage.get("backend") == "本地脚本" else "hybrid"
    state = "已验证" if stage.get("status") == "已接入" else "待接入" if stage.get("status") == "待接入" else "可调试"
    value["execution"] = stage.get("execution") if isinstance(stage.get("execution"), dict) else {"mode": mode, "label": stage.get("backend", "未指定"), "references": [stage.get("recipe", "")], "state": state}
    return value


@app.get("/api/pipeline/{stage_id}")
def pipeline_detail(stage_id: str) -> dict[str, Any]:
    """Return one independently inspectable production-step contract."""
    stage = next((item for item in PIPELINE_STAGES if item["id"] == stage_id), None)
    if stage is None:
        raise HTTPException(404, "制作步骤不存在")
    return {"schema_version": 2, "project": CURRENT_PROJECT_ID, "stage": pipeline_contract(stage)}


@app.post("/api/pipeline/{stage_id}/validate")
def validate_pipeline(stage_id: str, request: PipelineValidateRequest) -> dict[str, Any]:
    return public_validate_pipeline(stage_id, request)


@app.get("/api/projects/{project_id}/pipeline")
def project_pipeline(project_id: str) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    return {"schema_version": 2, "project": project_id, "stages": [pipeline_contract(stage, manifest) for stage in manifest.get('pipeline', [])]}


@app.post("/api/projects/{project_id}/pipeline/{stage_id}/validate")
def scoped_validate_pipeline(project_id: str, stage_id: str, request: PipelineValidateRequest) -> dict[str, Any]:
    return public_validate_pipeline(stage_id, request, require_project_manifest(project_id))


def public_validate_pipeline(stage_id, request, manifest=None):
    project = manifest or CURRENT_PROJECT
    try:
        if stage_id == 'auto':
            document, _ = load_project_plan(str(project['id']))
            segment = next((row for row in document.get('segments', []) if row.get('id') == request.asset_id), None)
            if not segment:
                raise HTTPException(404, '分镜不存在')
            _, selected = select_preset_execution(project, segment.get('workflow'), None)
            if not selected:
                raise HTTPException(422, {'code': 'creation_recipe_unresolved',
                    'message': '当前制作方式无法与已保存输入对应。请核对执行回执并修复配方身份；预检未修改作品。'})
            stage_id = selected
        return validate_project_pipeline(stage_id, request, manifest)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {'code': 'validation_failed', 'message': str(exc.detail)}
        document, _ = load_project_plan(str(project['id']))
        validation = {**detail, 'valid': False}
        if not validation.get('blockers'):
            validation['blockers'] = [{'code': detail['code'], 'message': detail['message'], 'action': 'edit_inputs'}]
        detail = {**detail, 'readiness': readiness.validation_readiness(validation, project_id=str(project['id']),
                    asset_id=request.asset_id or '', stage_id=stage_id, revision=document.get('revision', 0),
                    source='draft' if request.values else 'saved')}
        raise HTTPException(exc.status_code, detail) from exc


@plan_transaction
def validate_project_pipeline(stage_id: str, request: PipelineValidateRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else CURRENT_PROJECT
    stages = project.get('pipeline', []) if manifest is not None else PIPELINE_STAGES
    templates = project.get('task_templates', {}) if manifest is not None else TASK_TEMPLATES
    plan_path = resolve_registered_path(str(project['plan_path'])) if manifest is not None else PLAN_PATH
    default_workflow = (project.get('production_preset') or {}).get('workflow')
    stage = next((item for item in stages if item["id"] == stage_id), None)
    if stage is None:
        raise HTTPException(404, "制作步骤不存在")
    if not request.asset_id:
        return validate_pipeline_values(stage, request.values, manifest)
    payload = dict(templates.get(request.asset_id, {}))
    payload["asset_id"] = request.asset_id
    try:
        document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        document = {"segments": []}
    segment = next((item for item in document.get("segments", []) if isinstance(item, dict) and item.get("id") == request.asset_id), None)
    if isinstance(segment, dict) and segment.get("workflow") and not payload.get("workflow"):
        payload["workflow"] = str(segment["workflow"])
    if not payload.get("workflow") and not payload.get("prompt") and default_workflow:
        payload["workflow"] = default_workflow
    payload['workflow'], stage_id = select_preset_execution(project, payload.get('workflow'), stage_id)
    prepared = prepare_pipeline_payload(payload, stage_id, request.values, manifest)
    result = validate_pipeline_values(stage, prepared["pipeline_values"], manifest)
    result["binding_count"] = len(prepared.get("_graph_bindings", []))
    result["materials"] = prepared.get("materials", {})
    result["parameters"] = prepared.get("parameters", {})
    if isinstance(prepared.get('h3_duration'), dict):
        result['h3_duration'] = prepared['h3_duration']
    if prepared.get('creative_inspection'):
        result['creative_inspection'] = prepared['creative_inspection']
    if prepared.get('resolved_generation'):
        result['resolved_generation'] = prepared['resolved_generation']
    blockers = segment_dependency_blockers(document, segment or {})
    result['valid'] = not blockers
    result['blockers'] = blockers
    result['revision'] = document.get('revision', 0)
    result['source'] = 'draft' if request.values else 'saved'
    result['readiness'] = readiness.validation_readiness(result, project_id=str(project['id']), asset_id=request.asset_id,
                                                       stage_id=stage_id, revision=result['revision'], source=result['source'])
    return result


def segment_dependency_blockers(document: dict[str, Any], segment: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {str(item.get('id')): item for item in document.get('segments', []) if isinstance(item, dict)}
    blockers = []
    for dependency in segment.get('dependencies') or []:
        item = by_id.get(str(dependency))
        reference = (item or {}).get('video', {}).get('path')
        try:
            exists = bool(reference and resolve_registered_path(str(reference)).is_file())
        except (HTTPException, ValueError, OSError):
            exists = False
        if not exists:
            blockers.append({'code': 'waiting_dependency', 'message': f'等待前置分镜 {dependency} 的可读视频', 'dependency': str(dependency), 'action': 'inspect_dependency'})
    return blockers


def project_readiness(project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, _ = load_project_plan(project_id)
    stages = manifest.get('pipeline', [])
    with DB_LOCK, db() as connection:
        raw_tasks = connection.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC', (project_id,)).fetchall()
    task_values = [task_row(row) for row in raw_tasks]
    for task in task_values:
        task['queue'] = TASK_SCHEDULER.queue_info(task['id'])
    def select_stage(segment, available):
        workflow = segment.get('workflow') or (manifest.get('production_preset') or {}).get('workflow')
        _, selected = select_preset_execution(manifest, workflow, None)
        if selected:
            return selected
        contracts = [pipeline_contract(stage, manifest) for stage in available]
        executable = [stage for stage in contracts if (stage.get('execution') or {}).get('mode') == 'comfyui' and
                      any(item.get('kind') == '视频' for item in stage.get('output_specs', []))]
        matched = next((stage for stage in executable if workflow in (stage.get('execution') or {}).get('references', [])), None)
        return (matched or (executable[0] if executable else {})).get('id')
    result = readiness.build_project_readiness(project_id=project_id, plan=document, stages=stages, project_state=state,
        tasks=task_values, stage_selector=select_stage, assembly_asset_id=str(manifest.get('assembly_asset_id') or 'MASTER'),
        validate=lambda stage, asset, values: validate_project_pipeline(stage, PipelineValidateRequest(asset_id=asset, values=values), manifest),
        assembly_preflight=lambda: preflight_project_assembly(project_id))
    result['installed_stages'] = [{'id': stage['id'], 'title': stage.get('title', stage['id'])} for stage in stages]
    result['creation_mode'] = 'auto' if ((not document.get('segments') and manifest.get('default_creation_mode') == 'auto') or result['segments'] and all((segment.get('generation') or {}).get('mode') == 'auto' for segment in document.get('segments', []))) else 'advanced'
    selected_stages = {row.get('stage_id') for row in result['segments']}
    if not selected_stages:
        selected_stages = {select_stage({}, stages)}
    result['input_requirements'] = [spec for stage in stages if stage.get('id') in selected_stages
                                    for spec in pipeline_contract(stage, manifest).get('input_specs', [])]
    return result


@app.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 100", (CURRENT_PROJECT_ID,)).fetchall()
    result = [task_row(row) for row in rows]
    for task in result:
        task['queue'] = TASK_SCHEDULER.queue_info(task['id'])
    return result


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    task = get_current_project_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks")
@gpu_admission
def start(request: StartRequest) -> dict[str, Any]:
    return submit_project_task(request)


@app.post("/api/projects/{project_id}/tasks")
@gpu_admission
def start_project_task(project_id: str, request: StartRequest) -> dict[str, Any]:
    return submit_project_task(request, require_project_manifest(project_id))


def find_submission_task(project_id: str, key: str) -> dict[str, Any] | None:
    with DB_LOCK, db() as connection:
        row = connection.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND json_extract(payload, '$.submission_receipt.key') = ? ORDER BY created_at, id LIMIT 1",
            (project_id, key),
        ).fetchone()
    return task_row(row) if row else None


@app.get("/api/projects/{project_id}/submission-receipt")
def submission_receipt(project_id: str, key: str = Query(min_length=1, max_length=160)) -> dict[str, Any]:
    require_project_manifest(project_id)
    task = find_submission_task(project_id, key)
    if not task:
        raise HTTPException(404, {'code': 'submission_not_found', 'message': '尚未找到已落库任务；原请求可能仍在处理中，请勿换请求标识重复提交', 'retryable': True})
    return task


@plan_transaction
def submit_project_task(request: StartRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else CURRENT_PROJECT_ID
    submission_request = request.model_dump(exclude={'idempotency_key'}, mode='json')
    if request.expected_revision is None:
        submission_request.pop('expected_revision', None)
    if request.idempotency_key is not None:
        existing = find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission_request:
                raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '该请求标识已用于不同生成参数；请查询原任务或为新操作使用新标识', 'task_id': existing['id'], 'retryable': False})
            return existing
    plan_path = resolve_registered_path(str(project['plan_path'])) if manifest is not None else PLAN_PATH
    templates = project.get('task_templates', {}) if manifest is not None else TASK_TEMPLATES
    default_workflow = (project.get('production_preset') or {}).get('workflow')
    require_submission_capacity()
    asset_id = request.asset_id or str(project.get("default_task_asset_id", ""))
    ensure_asset_not_pending(project_id, asset_id)
    if not asset_id or asset_id == str(project.get("assembly_asset_id", "")):
        raise HTTPException(400, "当前素材不能作为单段生成任务；完整版请使用装配入口")
    payload = dict(templates.get(asset_id, {}))
    payload["asset_id"] = asset_id
    try:
        document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        document = {"segments": []}
    check_plan_revision(document, request.expected_revision)
    segment = next((item for item in document.get("segments", []) if isinstance(item, dict) and item.get("id") == asset_id), None)
    if isinstance(segment, dict) and (segment.get('generation') or {}).get('mode') == 'auto':
        payload['workflow'] = segment['workflow']
        _, resolved_stage = select_preset_execution(project, segment['workflow'], None)
        if request.pipeline_stage_id not in (None, 'auto', resolved_stage):
            raise HTTPException(422, '自动创作方式由已保存输入决定；使用不同技术方案前请明确切换高级模式。')
    if isinstance(segment, dict) and segment.get("workflow") and not payload.get("workflow"):
        payload["workflow"] = str(segment["workflow"])
    if not payload.get("workflow") and not payload.get("prompt") and default_workflow:
        payload["workflow"] = default_workflow
    payload['workflow'], stage_id = select_preset_execution(project, payload.get('workflow'), request.pipeline_stage_id)
    if not payload.get("workflow") and not payload.get("prompt"):
        raise HTTPException(400, f"项目没有登记 {asset_id} 的可执行工作流或任务模板")
    if stage_id:
        values = dict(request.pipeline_values)
        if request.prompt is not None:
            values["prompt"] = request.prompt
        if request.seed is not None and "seed" not in values:
            values["seed"] = request.seed
        payload = prepare_pipeline_payload(payload, stage_id, values, manifest)
    else:
        if request.prompt is not None:
            if payload.get("workflow"):
                payload["prompt_override"] = request.prompt
            else:
                payload["prompt"] = request.prompt
        elif payload.get("workflow") and isinstance(segment, dict) and segment.get("prompt"):
            payload["prompt_override"] = str(segment["prompt"])
        if request.seed is not None:
            payload["seed"] = request.seed
    if isinstance(segment, dict):
        payload["segment_snapshot"] = segment_generation_snapshot(segment)
        payload['segment_dependencies'] = list(segment.get('dependencies') or [])
    payload.update({"project_id": project_id, "plan_path": str(plan_path), "assembly_asset_id": str(project.get("assembly_asset_id", ""))})
    if request.idempotency_key is not None:
        payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission_request}
    try:
        payload = freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"无法冻结本次执行快照: {exc}") from exc
    task_id = str(uuid.uuid4())
    now = time.time()
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, asset_id, "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    queue_task(task_id)
    return get_task(task_id) or {"id": task_id, "status": "preparing"}


@app.get('/api/keyframe-capability')
def keyframe_capability(mode: Literal['reference', 'text'] = 'reference') -> dict[str, Any]:
    try:
        info = request_json('/object_info')
        if not isinstance(info, dict):
            raise ValueError('节点注册表格式无效')
        return keyframes.capability(info, mode)
    except (OSError, ValueError, HTTPError, URLError) as exc:
        return {'available': False, 'engine': 'Krea 2 Turbo' if mode == 'text' else 'Qwen Image Edit 2511', 'requires_reference_image': mode != 'text',
                'missing_nodes': [], 'missing_models': [], 'reason': f'无法读取 ComfyUI 节点与模型: {exc}'}


class KeyframeRequest(BaseModel):
    mode: Literal['reference', 'text'] = 'reference'
    prompt: str = Field(min_length=1, max_length=6000)
    reference_image: str | None = Field(default=None, min_length=1, max_length=1000)
    width: int = Field(default=768, ge=512, le=1536)
    height: int = Field(default=1344, ge=512, le=1536)
    seed: int = Field(default=340921, ge=0, le=2**64 - 1)
    idempotency_key: str = Field(min_length=1, max_length=160)


def complete_keyframe_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is not None:
        published = PRIVATE_WORKSPACES.publish(task, Path(receipt['output']), OUTPUT_ROOT)
        receipt = {**receipt, 'engine_output': receipt['output'], 'output': str(published)}
    with MATERIAL_REUSE_LOCK:
        manifest = require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        asset_id = f"keyframe-{task['id']}"
        if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
            source = root_relative_path(Path(receipt['output']))
            assets.append({'id': asset_id, 'kind': 'image', 'title': f"关键帧 · {task['payload']['request']['prompt'][:32]}",
                           'ready': True, 'subtitle': '关键帧候选 · 待审核', 'origin': task['payload']['request'].get('engine', 'Qwen Image Edit 2511'),
                           'sources': {'A': source}, 'image_path': source, 'keyframe_task_id': task['id']})
            manifest['assets'] = assets
            save_project_manifest(manifest)
            if task['project_id'] == CURRENT_PROJECT_ID:
                apply_project_manifest(manifest)
    set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(receipt, ensure_ascii=False))
    return get_task(task['id']) or task


@app.post('/api/projects/{project_id}/keyframe-tasks', status_code=202)
@gpu_admission
def start_keyframe_task(project_id: str, request: KeyframeRequest) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    submission = {'kind': 'keyframe', 'prompt': request.prompt, 'reference_image': request.reference_image, 'seed': request.seed}
    if request.mode == 'text':
        if request.reference_image is not None:
            raise HTTPException(422, '文字生图不接收参考图片，请选择参考图模式')
        submission.update(mode='text', width=request.width, height=request.height)
    elif not request.reference_image:
        raise HTTPException(422, '参考图模式需要选择图片')
    elif (request.width, request.height) != (768, 1344):
        raise HTTPException(422, '参考图模式的画幅由参考图片决定')
    existing = find_submission_task(project_id, request.idempotency_key)
    if existing:
        if existing['payload']['submission_receipt']['request'] != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    require_submission_capacity()
    readiness = keyframe_capability(mode='text') if request.mode == 'text' else keyframe_capability()
    if not readiness['available']:
        raise HTTPException(503, {'message': '关键帧生成环境尚未就绪', **readiness})
    task_id = str(uuid.uuid4())
    try:
        workspace = resolve_registered_path(str(manifest['workspace_root']))
        prefix = f'导演工作台工作目录/{project_id}/关键帧/{task_id}'
        if request.mode == 'text':
            payload = keyframes.prepare_text(task_id, request.prompt, workspace / 'keyframe-tasks', prefix, request.seed, request.width, request.height)
        else:
            reference = resolve_registered_path(request.reference_image)
            payload = keyframes.prepare(task_id, request.prompt, reference, workspace / 'keyframe-tasks', prefix, request.seed)
        payload = freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    payload.update(project_id=project_id, asset_id=f'keyframe-{task_id}',
                   submission_receipt={'key': request.idempotency_key, 'request': submission})
    now = time.time()
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, payload['asset_id'], 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    queue_task(task_id)
    return get_task(task_id)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=2000)
    mode: Literal['reference', 'design'] = 'reference'
    voice_reference: str | None = Field(default=None, min_length=1, max_length=1000)
    voice_description: str | None = Field(default=None, min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    gen_seconds: float = Field(default=4.5, gt=0, le=30, allow_inf_nan=False)
    seed: int = Field(default=20260923, ge=0, le=4294967295, strict=True)


    @model_validator(mode='after')
    def validate_voice_mode(self):
        if self.mode == 'reference':
            if not self.voice_reference or not self.voice_reference.strip() or self.voice_description is not None:
                raise ValueError('参考模式须提供音色参考，不能同时提供声音设计描述')
        elif not self.voice_description or not self.voice_description.strip() or self.voice_reference is not None:
            raise ValueError('设计模式须提供声音描述，不能同时提供参考音频')
        return self


@app.get('/api/speech-capability')
def speech_capability() -> dict[str, Any]:
    return speech.capability()


class MediaOperationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['silence', 'video_qc', 'transcribe', 'silent_video']
    idempotency_key: str = Field(min_length=1, max_length=160)
    source_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    duration_seconds: float | None = Field(default=None, gt=0, le=120, strict=True, allow_inf_nan=False)
    sample_rate: Literal[16000, 24000, 48000] = 24000
    channels: Literal[1, 2] = 1
    sample_count: int = Field(default=12, ge=3, le=60, strict=True)
    expected_text: str = Field(default='', max_length=2000)
    language: Literal['zh', 'en', 'auto'] = 'zh'
    word_timestamps: StrictBool = True

    @field_validator('sample_rate', 'channels', mode='before')
    @classmethod
    def integer_audio_format(cls, value):
        if type(value) is not int:
            raise ValueError('采样率和声道须为整数')
        return value

    @model_validator(mode='after')
    def operation_fields(self):
        fields = {
            'silence': {'duration_seconds', 'sample_rate', 'channels'},
            'video_qc': {'sample_count'},
            'transcribe': {'expected_text', 'language', 'word_timestamps'},
            'silent_video': set(),
        }[self.operation]
        extra = self.model_fields_set - fields - {'operation', 'idempotency_key', 'source_asset_id'}
        if extra:
            raise ValueError('此操作不接受参数: ' + ', '.join(sorted(extra)))
        if not self.idempotency_key.strip():
            raise ValueError('提交凭据不能为空')
        if self.operation == 'silence':
            if self.source_asset_id is not None or self.duration_seconds is None:
                raise ValueError('静音生成须指定时长且不接收源素材')
            if type(self.sample_rate) is not int or type(self.channels) is not int:
                raise ValueError('采样率和声道须为整数')
        elif self.source_asset_id is None or not self.source_asset_id.strip():
            raise ValueError('请选择本作品的源素材')
        return self

    def parameters(self) -> dict[str, Any]:
        fields = {'silence': ('duration_seconds', 'sample_rate', 'channels'),
                  'video_qc': ('sample_count',),
                  'transcribe': ('expected_text', 'language', 'word_timestamps'),
                  'silent_video': ()}[self.operation]
        return {field: getattr(self, field) for field in fields}


@app.get('/api/media-capability')
def media_capability() -> dict[str, Any]:
    return media_operations.capability()


def media_operation_source(manifest: dict[str, Any], asset_id: str, operation: str) -> tuple[Path, dict[str, Any]]:
    """Select only a current, registered project asset; callers cannot submit paths."""
    asset = next((a for a in manifest.get('assets', []) if isinstance(a, dict) and a.get('id') == asset_id), None)
    value = (asset.get('sources') or {}).get('A') if asset else None
    kind = asset.get('kind') if asset else None
    if not asset:
        plan, _ = load_project_plan(str(manifest['id']))
        segment = next((s for s in plan['segments'] if isinstance(s, dict) and s.get('id') == asset_id), None)
        if segment:
            value, kind = (segment.get('video') or {}).get('path'), 'video'
        elif asset_id == manifest.get('assembly_asset_id'):
            value, kind = (plan.get('assembly') or {}).get('output'), 'video'
    allowed = {'audio', 'video'} if operation == 'transcribe' else {'video'}
    if kind not in allowed or not isinstance(value, str) or not value:
        raise HTTPException(422, '请选择当前作品已有结果的音视频素材')
    try:
        source = resolve_registered_path(value)
        if not source.is_file():
            raise ValueError('源素材不存在')
        info = source.stat()
        if info.st_size > 2 * 1024**3:
            raise ValueError('首版媒体处理限制单个源文件不超过 2 GiB')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return source, {'asset_id': asset_id, 'kind': kind, 'source': value,
                    'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


@app.post('/api/projects/{project_id}/media-tasks', status_code=202)
def start_media_operation(project_id: str, request: MediaOperationRequest) -> dict[str, Any]:
    with MEDIA_OPERATION_LOCK:
        manifest = require_project_manifest(project_id)
        submission = {'kind': 'media_operation', 'operation': request.operation,
                      'source_asset_id': request.source_asset_id, 'parameters': request.parameters()}
        existing = find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
        require_submission_capacity()
        ready = media_operations.capability().get('operations', {}).get(request.operation, {})
        if not ready.get('available'):
            raise HTTPException(503, {'message': '媒体处理环境尚未就绪', **ready})
        source, source_record = (None, None) if request.operation == 'silence' else media_operation_source(manifest, request.source_asset_id, request.operation)
        task_id = str(uuid.uuid4())
        try:
            workspace = resolve_registered_path(str(manifest['workspace_root']))
            payload = media_operations.prepare(task_id, request.operation, source, workspace / 'media-tasks', parameters=request.parameters())
            owner = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        except (OSError, ValueError, psutil.Error, media_operations.MediaOperationFailed) as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update(project_id=project_id, source_asset=source_record, execution_owner=owner,
                       submission_receipt={'key': request.idempotency_key, 'request': submission})
        if PRIVATE_WORKSPACES is not None:
            payload['owner_user'] = PRIVATE_WORKSPACES.read()['projects'].get(project_id)
        now = time.time()
        with DB_LOCK, db() as connection:
            check_queue_limits(connection, settings_owner())
            connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                (task_id, f'media-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        queue_task(task_id)
        return get_task(task_id)


def complete_media_operation_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    with MEDIA_OPERATION_LOCK:
        task = get_task(task['id']) or task
        if task.get('stop_requested') or task['status'] == 'stopped':
            set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False), error='已停止；完成的文件保留，不登记为候选')
            return get_task(task['id'])
        outputs = []
        with MATERIAL_REUSE_LOCK:
            manifest = require_project_manifest(task['project_id'])
            assets = list(manifest.get('assets') or [])
            first = True
            for index, output in enumerate(receipt['outputs']):
                output = dict(output)
                diagnostic = task['payload']['operation'] == 'video_qc'
                if diagnostic:
                    output['purpose'] = 'diagnostic'
                if not diagnostic and output['kind'] in {'audio', 'video', 'image'}:
                    asset_id = task['asset_id'] if first else f"{task['asset_id']}-{index:02d}"
                    first = False
                    output['asset_id'] = asset_id
                    if not any(a.get('id') == asset_id for a in assets):
                        source = root_relative_path(Path(output['path']))
                        asset = {'id': asset_id, 'kind': output['kind'], 'title': output.get('title') or '媒体处理候选',
                                 'ready': True, 'subtitle': '媒体工具产物 · 待检查', 'origin': '工作台媒体工具',
                                 'sources': {'A': source}, 'media_task_id': task['id'],
                                 'operation': task['payload']['operation'], 'source_asset': task['payload'].get('source_asset')}
                        if output['kind'] == 'image':
                            asset['image_path'] = source
                        assets.append(asset)
                outputs.append(output)
            manifest['assets'] = assets
            save_project_manifest(manifest)
            if task['project_id'] == CURRENT_PROJECT_ID:
                apply_project_manifest(manifest)
        result = {**receipt, 'outputs': outputs}
        set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(result, ensure_ascii=False))
        return get_task(task['id'])


def run_media_operation_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        set_task(task_id, status='running')
        receipt = media_operations.execute(payload, lambda: bool((get_task(task_id) or {}).get('stop_requested')))
    except media_operations.MediaOperationStopped as exc:
        set_task(task_id, status='stopped', error=str(exc))
        return
    except (media_operations.MediaOperationFailed, ValueError) as exc:
        set_task(task_id, status='failed', error=f'媒体处理失败: {exc}')
        return
    except Exception as exc:
        set_task(task_id, status='needs_reconcile', error=f'媒体处理结果待核对: {exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        complete_media_operation_task(get_task(task_id), receipt)
    except Exception as exc:
        set_task(task_id, status='needs_reconcile', error=f'媒体工具产物登记待核对: {exc}')


class AudioEditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_asset_id: str = Field(min_length=1, max_length=160)
    background_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    start_seconds: float = Field(default=0, ge=0, le=86400, strict=True, allow_inf_nan=False)
    duration_seconds: float | None = Field(default=None, gt=0, le=120, strict=True, allow_inf_nan=False)
    pad_silence: StrictBool = False
    gain: float = Field(default=1, ge=0, le=8, strict=True, allow_inf_nan=False)
    fade_in_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)
    fade_out_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)
    background_gain: float = Field(default=.25, ge=0, le=8, strict=True, allow_inf_nan=False)
    background_offset_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)


def audio_edit_owner_alive(payload: dict[str, Any]) -> bool:
    if payload.get('execution_finished_at'):
        return False  # Written only after the synchronous CPU executor has returned.
    owner = payload.get('execution_owner') or {}
    if type(owner.get('pid')) is not int or type(owner.get('created_at')) not in (int, float):
        raise HTTPException(409, '缺少CPU执行进程身份，不能确认执行结束')
    try:
        return psutil.Process(owner['pid']).create_time() == owner['created_at']
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError) as exc:
        raise HTTPException(409, '无法核对CPU执行进程身份，保留待核对状态') from exc


def audio_edit_source(manifest: dict[str, Any], asset_id: str) -> tuple[Path, dict[str, Any]]:
    asset = next((item for item in manifest.get('assets', []) if isinstance(item, dict) and item.get('id') == asset_id and item.get('kind') == 'audio'), None)
    value = (asset.get('sources') or {}).get('A') if asset else None
    if not isinstance(value, str) or not value:
        raise HTTPException(422, '请选择当前作品已登记且有A来源的音频素材')
    try:
        path = resolve_registered_path(value)
        if not path.is_file():
            raise ValueError('已登记音频文件不存在')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return path, {'asset_id': asset_id, 'source': value, 'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}


@app.post('/api/projects/{project_id}/audio-edit-tasks', status_code=202)
def start_audio_edit_task(project_id: str, request: AudioEditRequest) -> dict[str, Any]:
    with AUDIO_EDIT_LOCK:
        manifest = require_project_manifest(project_id)
        submission = {'kind': 'audio_edit', **request.model_dump(exclude={'idempotency_key'})}
        if not request.pad_silence:
            submission.pop('pad_silence', None)  # Preserve old default-request idempotency receipts.
        if not request.idempotency_key.strip():
            raise HTTPException(422, '请求标识不能为空')
        existing = find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
        require_submission_capacity()
        source, source_record = audio_edit_source(manifest, request.source_asset_id)
        background, background_record = (None, None) if request.background_asset_id is None else audio_edit_source(manifest, request.background_asset_id)
        task_id = str(uuid.uuid4())
        try:
            workspace = resolve_registered_path(str(manifest['workspace_root']))
            payload = audio_edit_tasks.prepare(task_id, source, workspace / 'audio-edit-tasks',
                parameters={key: submission.get(key, default) for key, default in audio_edit_tasks.DEFAULTS.items()}, background=background)
            owner = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        except (OSError, ValueError, psutil.Error) as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update(project_id=project_id, source_asset=source_record, background_asset=background_record,
                       execution_owner=owner, submission_receipt={'key': request.idempotency_key, 'request': submission})
        if PRIVATE_WORKSPACES is not None:
            payload['owner_user'] = PRIVATE_WORKSPACES.read()['projects'].get(project_id)
        now = time.time()
        with DB_LOCK, db() as connection:
            check_queue_limits(connection, settings_owner())
            connection.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                (task_id, f'audio-edit-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
            connection.commit()
        queue_task(task_id)
        return get_task(task_id)


def complete_audio_edit_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    with AUDIO_EDIT_LOCK:
        task = get_task(task['id']) or task
        if task.get('stop_requested') or task['status'] == 'stopped':
            set_task(task['id'], status='stopped', result=json.dumps(receipt, ensure_ascii=False), error='CPU处理已结束；已按停止请求保留结果但不登记候选')
            return get_task(task['id'])
        with MATERIAL_REUSE_LOCK:
            manifest = require_project_manifest(task['project_id'])
            assets = list(manifest.get('assets') or [])
            asset_id = f"audio-edit-{task['id']}"
            if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
                assets.append({'id': asset_id, 'kind': 'audio', 'title': '编辑音频', 'ready': True,
                    'subtitle': '编辑音频候选 · 待试听', 'origin': 'CPU音频编辑',
                    'sources': {'A': root_relative_path(Path(receipt['output']))}, 'start': 0, 'end': receipt['duration_seconds'],
                    'audio_edit_task_id': task['id'], 'source_asset': task['payload']['source_asset'],
                    'background_asset': task['payload'].get('background_asset'), 'edit_parameters': task['payload']['request']['parameters']})
                manifest['assets'] = assets
                save_project_manifest(manifest)
                if task['project_id'] == CURRENT_PROJECT_ID:
                    apply_project_manifest(manifest)
        set_task(task['id'], status='succeeded', stop_requested=0, error=None, result=json.dumps(receipt, ensure_ascii=False))
        return get_task(task['id'])


def run_audio_edit_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        receipt = audio_edit_tasks.execute(payload, lambda: bool((get_task(task_id) or {}).get('stop_requested')))
    except audio_edit_tasks.AudioEditStoppedBeforeStart as exc:
        set_task(task_id, status='stopped', error=str(exc))
        return
    except ValueError as exc:
        set_task(task_id, status='failed', error=f'CPU音频编辑校验失败: {exc}')
        return
    except Exception as exc:
        set_task(task_id, status='needs_reconcile', error=f'CPU音频编辑结果待核对: {exc}')
        return
    finally:
        payload = {**payload, 'execution_finished_at': time.time()}
        set_task(task_id, payload=json.dumps(payload, ensure_ascii=False))
    try:
        complete_audio_edit_task(get_task(task_id), receipt)
    except Exception as exc:
        set_task(task_id, status='needs_reconcile', error=f'CPU音频候选登记待核对: {exc}')


def complete_speech_task(task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    # Keep the candidate independent: do not overwrite a shot's selected audio.
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_task(task)
    with MATERIAL_REUSE_LOCK:
        manifest = require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        asset_id = f"speech-{task['id']}"
        if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
            assets.append({'id': asset_id, 'kind': 'audio', 'title': f"台词 · {task['payload']['request']['text'][:32]}",
                           'ready': True, 'subtitle': '声音候选 · 待试听',
                           'origin': 'AuK-Flash' if task['payload'].get('engine') == 'auk-flash' else 'IndexTTS 2.5',
                           'sources': {'A': root_relative_path(Path(receipt['output']))},
                           'start': 0, 'end': receipt['duration_seconds'],
                           'speech_task_id': task['id']})
            manifest['assets'] = assets
            save_project_manifest(manifest)
            if task['project_id'] == CURRENT_PROJECT_ID:
                apply_project_manifest(manifest)
    set_task(task['id'], status='succeeded', stop_requested=0, error=None,
             result=json.dumps(receipt, ensure_ascii=False))
    return get_task(task['id']) or task


def run_speech_task(task_id: str, payload: dict[str, Any]) -> None:
    try:
        if PRIVATE_WORKSPACES is not None:
            PRIVATE_WORKSPACES.verify_task({'project_id': payload.get('project_id'), 'payload': payload})
        receipt = speech.execute(payload, lambda: bool((get_task(task_id) or {}).get('stop_requested')))
        complete_speech_task(get_task(task_id), receipt)
    except speech.SpeechStopped as exc:
        set_task(task_id, status='stopped', error=str(exc))
    except speech.SpeechFailed as exc:
        set_task(task_id, status='failed', error=str(exc))
    except Exception as exc:
        # A process/receipt error must never enable an automatic duplicate render.
        set_task(task_id, status='needs_reconcile', error=str(exc))


@app.post('/api/projects/{project_id}/speech-tasks', status_code=202)
@gpu_admission
def start_speech_task(project_id: str, request: SpeechRequest) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    submission = {'kind': 'speech', 'text': request.text}
    if request.mode == 'design':
        submission.update(mode='design', voice_description=request.voice_description)
    else:
        submission['voice_reference'] = request.voice_reference
    existing = find_submission_task(project_id, request.idempotency_key)
    if existing:
        previous = existing['payload']['submission_receipt']['request']
        if 'engine' in previous:
            submission.update(engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed)
        elif request.model_fields_set & {'gen_seconds', 'seed'}:
            raise HTTPException(409, '旧声音请求不能追加新模型参数，请查询原任务')
        if previous != submission:
            raise HTTPException(409, '请求标识已用于不同参数')
        return existing
    submission.update(engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed)
    readiness = speech.capability()
    if not readiness['available']:
        raise HTTPException(503, {'message': '声音生成环境尚未就绪', 'missing': readiness['missing']})
    require_submission_capacity()
    try:
        reference = resolve_registered_path(request.voice_reference) if request.mode == 'reference' else None
        workspace = resolve_registered_path(str(manifest['workspace_root']))
        task_id = str(uuid.uuid4())
        payload = speech.prepare(task_id, request.text, reference, workspace / 'speech-tasks',
                                 engine='auk-flash', gen_seconds=request.gen_seconds, seed=request.seed,
                                 mode=request.mode, voice_description=request.voice_description)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    payload.update(project_id=project_id, submission_receipt={'key': request.idempotency_key, 'request': submission})
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    now = time.time()
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute('INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (task_id, f'speech-{task_id}', 'scheduler_waiting', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    queue_task(task_id)
    return get_task(task_id)


@app.post("/api/batches")
@gpu_admission
def start_batch(request: BatchRequest) -> dict[str, Any]:
    return submit_project_batch(request)


@app.post("/api/projects/{project_id}/batches")
@gpu_admission
def start_project_batch(project_id: str, request: BatchRequest) -> dict[str, Any]:
    return submit_project_batch(request, require_project_manifest(project_id))


@plan_transaction
def submit_project_batch(request: BatchRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else CURRENT_PROJECT_ID
    submission_request = request.model_dump(exclude={'idempotency_key'}, mode='json')
    if request.expected_revision is None:
        submission_request.pop('expected_revision', None)
    if request.idempotency_key:
        existing = find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission_request or not existing.get('batch_id'):
                raise HTTPException(409, {'code': 'idempotency_key_conflict', 'message': '请求标识已用于不同参数', 'retryable': False})
            with DB_LOCK, db() as connection:
                rows = connection.execute('SELECT id FROM tasks WHERE batch_id=? ORDER BY sequence', (existing['batch_id'],)).fetchall()
            return {'batch_id': existing['batch_id'], 'task_ids': [row['id'] for row in rows], 'status': 'accepted'}
    plan_path = resolve_registered_path(str(project['plan_path'])) if manifest is not None else PLAN_PATH
    default_workflow = (project.get('production_preset') or {}).get('workflow')
    require_submission_capacity()
    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"无法读取分段计划: {exc}") from exc
    check_plan_revision(plan_document, request.expected_revision)
    plan_segments = {str(item.get("id")): item for item in plan_document.get("segments", []) if isinstance(item, dict) and item.get("id")}
    requested_ids = request.asset_ids or [asset_id for asset_id, segment in plan_segments.items() if segment.get("workflow") or default_workflow]
    if not requested_ids:
        raise HTTPException(400, "当前项目计划没有可提交的片段工作流")
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(422, "批次不能重复包含同一个分镜")
    unsupported = sorted(set(requested_ids) - set(plan_segments))
    if unsupported:
        raise HTTPException(400, f"计划中不存在这些片段: {', '.join(unsupported)}")
    batch_positions = {asset_id: index for index, asset_id in enumerate(requested_ids)}
    for asset_id in requested_ids:
        later_dependencies = [str(dependency) for dependency in plan_segments[asset_id].get('dependencies', [])
                              if str(dependency) in batch_positions and
                              batch_positions[str(dependency)] >= batch_positions[asset_id]]
        if later_dependencies:
            raise HTTPException(422, {'code': 'batch_dependency_order', 'asset_id': asset_id,
                                      'dependencies': later_dependencies,
                                      'message': '请将批次中的前置分镜放在依赖它的分镜之前',
                                      'action': 'reorder_batch'})
    for asset_id in requested_ids:
        ensure_asset_not_pending(project_id, asset_id)
    batch_id = str(uuid.uuid4())
    now = time.time()
    task_ids: list[str] = []
    payloads: list[dict[str, Any]] = []
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner(), len(requested_ids))
        for sequence, asset_id in enumerate(requested_ids, start=1):
            segment = plan_segments[asset_id]
            if (segment.get('generation') or {}).get('mode') == 'auto':
                _, resolved_stage = select_preset_execution(project, segment.get('workflow'), None)
                if request.pipeline_stage_id not in (None, 'auto', resolved_stage):
                    raise HTTPException(422, '自动分镜需按已保存输入生成；更换技术步骤前请切换高级模式。')
            workflow = segment.get("workflow") or default_workflow
            if not workflow:
                raise HTTPException(400, f"{asset_id} 尚未登记可提交的 ComfyUI 工作流")
            payload = {
                "asset_id": asset_id,
                "workflow": str(workflow),
                "project_id": project_id,
                "plan_path": str(plan_path),
                "assembly_asset_id": str(project.get("assembly_asset_id", "")),
                "segment_snapshot": segment_generation_snapshot(segment),
            }
            prompt = request.prompt if request.prompt is not None else segment.get("prompt")
            payload['workflow'], stage_id = select_preset_execution(project, payload.get('workflow'), request.pipeline_stage_id)
            if stage_id:
                values = dict(request.pipeline_values_by_asset.get(asset_id, {}))
                if prompt is not None and "prompt" not in values:
                    values["prompt"] = str(prompt)
                payload = prepare_pipeline_payload(payload, stage_id, values, manifest)
            elif prompt is not None:
                payload["prompt_override"] = str(prompt)
            try:
                payload = freeze_task_payload(payload, project_id=project_id)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(422, f"{asset_id} 无法冻结执行快照: {exc}") from exc
            task_id = str(uuid.uuid4())
            if request.idempotency_key:
                payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission_request}
            payload['segment_dependencies'] = list(segment.get('dependencies') or [])
            task_ids.append(task_id); payloads.append(payload)
            connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, stop_requested, payload, batch_id, sequence, project_id) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)", (task_id, asset_id, "batch_waiting", now, now, json.dumps(payload, ensure_ascii=False), batch_id, sequence, project_id))
        connection.commit()
    TASK_SCHEDULER.wake()
    return {"batch_id": batch_id, "task_ids": task_ids, "status": "accepted"}


class AssemblyJoinReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    status: Literal['pending', 'approved', 'redo_left', 'redo_right']
    constraint: str = Field(default='', max_length=2000)
    note: str = Field(default='', max_length=2000)


class AssemblySoundRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    policy: Literal['segment_native', 'complete_master', 'mute']
    audio_asset_id: str | None = Field(default=None, max_length=160)


@app.get('/api/projects/{project_id}/assembly/edit')
def get_project_assembly_edit(project_id: str) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, _ = load_project_plan(project_id)
    try:
        return assembly_edit_view(manifest, document)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put('/api/projects/{project_id}/assembly/joins/{left_id}/{right_id}')
@plan_transaction
def save_project_assembly_join(project_id: str, left_id: str, right_id: str,
                               request: AssemblyJoinReviewRequest) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    view = assembly_edit_view(manifest, document)
    join = next((item for item in view['joins']
                 if item['left_id'] == left_id and item['right_id'] == right_id), None)
    if join is None:
        raise HTTPException(422, '所选分镜不是当前计划中相邻的接点')
    if request.status == 'approved' and not join['review_ready']:
        raise HTTPException(422, '接点两侧均需有当前视频才能审核通过')
    edit = document.setdefault('assembly_edit', {})
    edit.setdefault('joins', {})[join['key']] = {
        'status': request.status, 'constraint': request.constraint.strip(),
        'note': request.note.strip(), 'signature': join['signature'],
        'reviewed_at': time.time(),
    }
    if request.status != 'approved' and isinstance(document.get('assembly'), dict) and (document['assembly'] or {}).get('output'):
        document['assembly'].update(status='stale', notes='接点审核要求修改，旧成片保留但需重新装配')
    write_plan_version(plan_path, document)
    return assembly_edit_view(manifest, document)


@app.put('/api/projects/{project_id}/assembly/sound')
@plan_transaction
def save_project_assembly_sound(project_id: str, request: AssemblySoundRequest) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, plan_path = load_project_plan(project_id)
    check_plan_revision(document, request.expected_revision)
    if request.policy == 'complete_master' and not request.audio_asset_id:
        raise HTTPException(422, '统一音轨须选择当前作品已登记的音频素材')
    if request.policy in {'segment_native', 'mute'} and request.audio_asset_id:
        raise HTTPException(422, '保留片段原音或静音时不能指定全片音轨')
    edit = document.setdefault('assembly_edit', {})
    edit['sound'] = {'policy': request.policy, 'audio_asset_id': request.audio_asset_id}
    try:
        assembly_sound_selection(manifest, document)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    persist_plan_edit(project_id, document, plan_path)
    return assembly_edit_view(manifest, document)


@app.get('/api/projects/{project_id}/assembly/preflight')
def preflight_project_assembly(project_id: str) -> dict[str, Any]:
    manifest = require_project_manifest(project_id)
    document, _ = load_project_plan(project_id)
    try:
        view = assembly_edit_view(manifest, document)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    builder_blocker = assembly_builder_blocker(manifest)
    if builder_blocker:
        view['blockers'].append(builder_blocker)
    view['ready'] = bool(document.get('segments')) and not view['blockers'] and view['ready']
    return view


def assembly_video_blockers(document):
    blockers = []
    for segment in document.get('segments', []):
        if not isinstance(segment, dict):
            continue
        asset_id = str(segment.get('id'))
        reference = (segment.get('video') or {}).get('path')
        try:
            exists = bool(reference and resolve_registered_path(str(reference)).is_file())
        except (HTTPException, ValueError, OSError):
            exists = False
        if not exists:
            blockers.append({'code': 'video_required', 'message': f'{asset_id} 当前生成视频不存在', 'dependency': asset_id})
        current = segment.get('current_version_task_id')
        version = next((item for item in segment.get('generation_history', []) if item.get('task_id') == current), None) if current else None
        saved_duration = (version or {}).get('snapshot', {}).get('duration_seconds')
        if saved_duration is not None and float(saved_duration) != float(segment.get('duration_seconds', 0)):
            blockers.append({'code': 'video_duration_stale', 'message': f'{asset_id} 当前视频按旧时长 {saved_duration} 秒生成；请先重新生成或恢复匹配的分镜时长', 'dependency': asset_id})
    return blockers


class AssemblyRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)


@app.post("/api/assembly")
@gpu_admission
def start_assembly(request: AssemblyRequest | None = None) -> dict[str, Any]:
    return submit_project_assembly(request=request)


@app.post("/api/projects/{project_id}/assembly")
@gpu_admission
def start_project_assembly(project_id: str, request: AssemblyRequest | None = None) -> dict[str, Any]:
    return submit_project_assembly(require_project_manifest(project_id), request)


@plan_transaction
def submit_project_assembly(manifest: dict[str, Any] | None = None, request: AssemblyRequest | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else CURRENT_PROJECT
    project_id = str(project['id']) if manifest is not None else CURRENT_PROJECT_ID
    request = request or AssemblyRequest()
    submission = {'kind': 'assembly', 'expected_revision': request.expected_revision}
    if request.idempotency_key:
        existing = find_submission_task(project_id, request.idempotency_key)
        if existing:
            if existing['payload']['submission_receipt']['request'] != submission:
                raise HTTPException(409, '请求标识已用于不同参数')
            return existing
    plan_path = resolve_registered_path(str(project['plan_path'])) if manifest is not None else PLAN_PATH
    stages = project.get('pipeline', []) if manifest is not None else PIPELINE_STAGES
    require_submission_capacity()
    plan_document, _ = load_project_plan(project_id)
    check_plan_revision(plan_document, request.expected_revision)
    builder_blocker = assembly_builder_blocker(manifest)
    if builder_blocker:
        raise HTTPException(422, {'code': builder_blocker['code'], 'message': builder_blocker['message'], 'blockers': [builder_blocker]})
    blocking = assembly_edit.blocking_joins(plan_document)
    if blocking:
        raise HTTPException(409, f"接点审核未通过或已过期: {', '.join(blocking)}")
    segment_ids = [str(item.get("id")) for item in plan_document.get("segments", []) if isinstance(item, dict) and item.get("id")]
    if not segment_ids:
        raise HTTPException(422, "装配需要至少一个分镜")
    missing_video = [asset_id for asset_id in segment_ids if not next(
        ((item.get("video") or {}).get("path") for item in plan_document["segments"]
         if isinstance(item, dict) and str(item.get("id")) == asset_id), None
    )]
    if missing_video:
        raise HTTPException(409, f"{len(missing_video)} 个分镜尚无当前生成视频")
    try:
        workflow_path = build_assembly_snapshot(manifest) if manifest is not None else build_assembly_snapshot()
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"无法根据最新分段计划生成装配工作流: {exc}") from exc
    active_statuses = tuple(task_scheduler.WAITING | task_scheduler.EXECUTING)
    assembly_asset_id = str(project.get("assembly_asset_id", "MASTER"))
    placeholders = ",".join("?" for _ in active_statuses)
    with DB_LOCK, db() as connection:
        active = connection.execute(f"SELECT id FROM tasks WHERE project_id = ? AND asset_id = ? AND status IN ({placeholders})", (project_id, assembly_asset_id, *active_statuses)).fetchone()
    if active:
        raise HTTPException(409, "完整版装配已经在运行")
    task_id = str(uuid.uuid4())
    now = time.time()
    payload = {"asset_id": assembly_asset_id, "workflow": str(workflow_path), "source_plan": str(plan_path), "plan_path": str(plan_path), "assembly_asset_id": assembly_asset_id, "project_id": project_id,
               "assembly_edit": {'joins': assembly_edit.joins(plan_document), 'sound': assembly_sound_selection(project, plan_document)}}
    previous_assembly = plan_document.get('assembly') or {}
    payload['previous_assembly_candidate'] = {key: previous_assembly.get(key) for key in ('prompt_id', 'output')}
    if request.idempotency_key:
        payload['submission_receipt'] = {'key': request.idempotency_key, 'request': submission}
    try:
        payload = freeze_task_payload(payload, project_id=project_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"无法冻结装配执行快照: {exc}") from exc
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, assembly_asset_id, "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    queue_task(task_id)
    return get_task(task_id) or {"id": task_id, "asset_id": assembly_asset_id, "status": "preparing"}


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str) -> dict[str, Any]:
    return stop_project_batch_tasks(CURRENT_PROJECT_ID, batch_id)


@app.post("/api/projects/{project_id}/batches/{batch_id}/stop")
def stop_project_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    require_project_manifest(project_id)
    return stop_project_batch_tasks(project_id, batch_id)


def stop_project_batch_tasks(project_id: str, batch_id: str) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT id,status FROM tasks WHERE project_id=? AND batch_id=?", (project_id, batch_id)).fetchall()
        if not rows:
            raise HTTPException(404, "批次不存在")
        if any(row['status'] == 'needs_reconcile' for row in rows):
            raise HTTPException(409, "批次有待核对任务，请先核对原回执")
        connection.execute("UPDATE tasks SET status='stopped',stop_requested=1,updated_at=?,error='批次已停止，未提交到 ComfyUI' WHERE project_id=? AND batch_id=? AND status='batch_waiting'", (time.time(), project_id, batch_id))
        connection.execute("UPDATE tasks SET status='stop_requested',stop_requested=1,updated_at=? WHERE project_id=? AND batch_id=? AND status IN ('preparing','submitting','queued','running','stopping','stop_requested')", (time.time(), project_id, batch_id))
        connection.commit()
        updated = connection.execute("SELECT * FROM tasks WHERE project_id=? AND batch_id=? ORDER BY sequence", (project_id, batch_id)).fetchall()
    return {"batch_id": batch_id, "tasks": [task_row(row) for row in updated]}


@app.post("/api/batches/{batch_id}/resume")
@gpu_admission
def resume_batch(batch_id: str) -> dict[str, Any]:
    return resume_project_batch_tasks(CURRENT_PROJECT_ID, batch_id)


@app.post("/api/projects/{project_id}/batches/{batch_id}/resume")
@gpu_admission
def resume_project_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    return resume_project_batch_tasks(project_id, batch_id, require_project_manifest(project_id))


def resume_project_batch_tasks(project_id: str, batch_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? AND batch_id = ? ORDER BY sequence", (project_id, batch_id)).fetchall()
    if not rows:
        raise HTTPException(404, "批次不存在")
    for row in rows:
        if row["status"] == "needs_reconcile" or (row["status"] in {"stopped", "failed"} and row["prompt_id"]):
            reconcile_task(task_in_project_context(task_row(row), manifest) if manifest is not None else task_row(row))
        elif row["status"] not in {"succeeded", "failed", "stopped"}:
            raise HTTPException(409, "批次仍在运行或停止中，不能重复恢复")
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? AND batch_id = ? ORDER BY sequence", (project_id, batch_id)).fetchall()
    pending = [row for row in rows if row["status"] != "succeeded"]
    if not pending:
        raise HTTPException(409, "批次已经全部完成，无需恢复")
    require_submission_capacity()
    new_batch_id = str(uuid.uuid4())
    now = time.time()
    task_ids: list[str] = []
    payloads = [(task_in_project_context(task_row(row), manifest)['payload'] if manifest is not None else json.loads(row['payload'])) for row in pending]
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner(), len(pending))
        for sequence, row in enumerate(pending, start=1):
            new_id = str(uuid.uuid4())
            task_ids.append(new_id)
            connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, stop_requested, payload, batch_id, sequence, project_id) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)", (new_id, row["asset_id"], "batch_waiting", now, now, json.dumps(payloads[sequence - 1], ensure_ascii=False), new_batch_id, sequence, row["project_id"]))
        connection.commit()
    TASK_SCHEDULER.wake()
    return {"batch_id": new_batch_id, "task_ids": task_ids, "resumed_from": batch_id, "status": "started"}


def gpu_idle_reason() -> str | None:
    """Inspect every user's GPU work and ComfyUI's whole queue; unknown is an error."""
    if REFERENCE_DECOMPOSE_ACTIVE or REVERSE_SEMANTIC_LOCK.locked():
        return "参考分析仍在运行"
    release_statuses = tuple(status for status in RELEASE_BLOCKING_STATUSES if status not in task_scheduler.WAITING)
    placeholders = ",".join("?" for _ in release_statuses)
    with DB_LOCK, db() as connection:
        rows = connection.execute(f"SELECT payload FROM tasks WHERE status IN ({placeholders})", release_statuses).fetchall()
    if any(json.loads(row['payload']).get('kind') not in {'audio_edit', 'media_operation'} for row in rows):
        return "工作台 GPU 任务仍在执行或待核对"
    with background_jobs.LOCK:
        jobs = background_jobs.records(analysis_job_root())
    if any(job['status'] in background_jobs.ACTIVE and not job.get('task_id') for job in jobs):
        return "参考语义分析任务仍在执行或待核对"
    if TASK_SCHEDULER.runnable_waiting():
        return '已有可派发 GPU 任务等待资源'
    queue = request_json('/queue')
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise ValueError('ComfyUI 队列回执不完整')
    if queue['queue_running'] or queue['queue_pending']:
        return "ComfyUI 队列仍有运行或待处理任务"
    return None


def gpu_free_mib() -> int | None:
    stats = request_json('/system_stats')
    if not isinstance(stats, dict) or not isinstance(stats.get('devices'), list) or not stats['devices']:
        return None
    free_bytes = stats['devices'][0].get('vram_free')
    return round(float(free_bytes) / 2**20) if free_bytes is not None else None


def comfy_history_token() -> str:
    """Notice a short direct-ComfyUI job even when both queue polls miss it."""
    history = request_json('/history?max_items=1')
    if not isinstance(history, dict) or len(history) > 1 or any(not isinstance(item, dict) for item in history.values()):
        raise ValueError('ComfyUI 最近任务历史回执不完整')
    return next(iter(history), '')


def request_comfy_free() -> dict[str, Any]:
    return request_json('/free', {'unload_models': True, 'free_memory': True})


def gpu_idle_setting(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError('must be positive')
        return value
    except ValueError:
        logging.warning('Invalid %s; using %.0f seconds', name, default)
        return default


GPU_IDLE_RELEASER = gpu_idle_release.IdleGPURelease(
    GPU_ADMISSION_LOCK, gpu_idle_reason, request_comfy_free, gpu_free_mib,
    grace_seconds=gpu_idle_setting('DIRECTOR_GPU_IDLE_GRACE_SECONDS', 120),
    poll_seconds=gpu_idle_setting('DIRECTOR_GPU_IDLE_POLL_SECONDS', 15),
    retry_seconds=gpu_idle_setting('DIRECTOR_GPU_IDLE_RETRY_SECONDS', 60),
    activity_token=comfy_history_token,
)


def check_queue_limits(connection, owner, count=1):
    try:
        TASK_SCHEDULER.check_limits(connection, owner, count)
    except task_scheduler.QueueLimitError as exc:
        raise HTTPException(429, exc.detail) from exc


def queue_task(task_id: str) -> None:
    TASK_SCHEDULER.wake()


def ensure_asset_not_pending(project_id: str, asset_id: str) -> None:
    statuses = tuple(task_scheduler.WAITING | task_scheduler.EXECUTING)
    with DB_LOCK, db() as connection:
        row = connection.execute(f"SELECT id FROM tasks WHERE project_id=? AND asset_id=? AND status IN ({','.join('?' for _ in statuses)})",
                                 (project_id, asset_id, *statuses)).fetchone()
    if row:
        raise HTTPException(409, {'code': 'asset_task_pending', 'message': '此分镜已有等待、执行或待核对任务，请查看原回执', 'task_id': row['id'], 'retryable': False})


def scheduler_resource_reason():
    if REFERENCE_DECOMPOSE_ACTIVE or REVERSE_SEMANTIC_LOCK.locked():
        return {'code': 'reference_running', 'message': '等待参考分析释放共享资源'}
    with background_jobs.LOCK:
        jobs = background_jobs.records(analysis_job_root())
    if any(job['status'] in background_jobs.ACTIVE and not job.get('task_id') for job in jobs):
        return {'code': 'analysis_needs_reconcile', 'message': '等待旧参考分析结束或核对回执'}
    queue = request_json('/queue')
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise ValueError('ComfyUI queue unavailable')
    if queue['queue_running'] or queue['queue_pending']:
        return {'code': 'external_queue_busy', 'message': '等待 ComfyUI 当前任务结束，不抢占外部任务'}
    return None


def revalidate_queued_task(task):
    payload = task['payload']
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_task(task)
    manifest = load_project_manifest(task['project_id'])
    if payload.get('workflow') and not resolve_registered_path(payload['workflow']).is_file():
        raise ValueError('冻结工作流无法读取，请重新预检')
    for material in (payload.get('execution_snapshot') or {}).get('staged_materials', []):
        path = Path(material['path'])
        if not path.is_file() or path.stat().st_size != material['size_bytes']:
            raise ValueError('冻结素材缺失或内容大小变化，请重新预检')
    for key in ('source_asset', 'background_asset'):
        record = payload.get(key)
        if record:
            path = resolve_registered_path(record['source'])
            if not path.is_file() or path.stat().st_size != record['size'] or path.stat().st_mtime_ns != record['mtime_ns']:
                raise ValueError('冻结素材已改变，请重新登记输入')
    if payload.get('kind') in {'reference_semantic', 'reference_decomposition'}:
        key = 'analysis_path' if payload['kind'] == 'reference_semantic' else 'source'
        if not resolve_registered_path(payload[key]).is_file():
            raise ValueError('参考分析输入无法读取')
    if payload.get('segment_dependencies'):
        document = json.loads(resolve_registered_path(manifest['plan_path']).read_text(encoding='utf-8-sig'))
        blockers = segment_dependency_blockers(document, {'dependencies': payload['segment_dependencies']})
        if blockers:
            return blockers[0]
    return None


def execute_scheduled_task(task):
    payload = task['payload']
    kind = payload.get('kind')
    if kind == 'speech':
        return run_speech_task(task['id'], payload)
    if kind == 'audio_edit':
        return run_audio_edit_task(task['id'], payload)
    if kind == 'media_operation':
        return run_media_operation_task(task['id'], payload)
    if kind == 'reference_semantic':
        job = dict(payload)
        if not REVERSE_SEMANTIC_LOCK.acquire(blocking=False):
            raise RuntimeError('Reference execution slot changed')
        set_task(task['id'], status='running')
        background_jobs.update(analysis_job_root(), job, status='running', owner=background_jobs.SESSION)
        try:
            execute_reverse_semantic(task['project_id'], payload['analysis_id'], Path(payload['analysis_path']), job)
            status = 'stopped' if (get_task(task['id']) or {}).get('stop_requested') else 'succeeded'
            set_task(task['id'], status=status)
            background_jobs.update(analysis_job_root(), job, status=status, owner=background_jobs.SESSION)
        except ReferenceStopped as exc:
            set_task(task['id'], status='stopped', error=str(exc))
            background_jobs.update(analysis_job_root(), job, status='stopped', error=str(exc), owner=background_jobs.SESSION)
        except HTTPException as exc:
            set_task(task['id'], status='failed', error=str(exc.detail))
            background_jobs.update(analysis_job_root(), job, status='failed', error=str(exc.detail), owner=background_jobs.SESSION)
        except Exception as exc:
            set_task(task['id'], status='needs_reconcile', error=str(exc))
            background_jobs.update(analysis_job_root(), job, status='needs_reconcile', error=str(exc))
        return
    if kind == 'reference_decomposition':
        set_task(task['id'], status='running')
        try:
            run_tracked_reference_decomposition(payload['command'], task_id=task['id'])
        except ReferenceStopped as exc:
            set_task(task['id'], status='stopped', error=str(exc))
            return
        except HTTPException as exc:
            set_task(task['id'], status='failed', error=str(exc.detail))
            return
        document, _ = read_reverse_analysis(task['project_id'], payload['analysis_id'])
        status = 'stopped' if (get_task(task['id']) or {}).get('stop_requested') else 'succeeded'
        set_task(task['id'], status=status, result=json.dumps(document, ensure_ascii=False))
        return
    runner = run_workflow_submission if payload.get('workflow') else run_submission
    runner(task['id'], payload)


TASK_SCHEDULER = task_scheduler.TaskScheduler(lambda: db(), DB_LOCK, GPU_ADMISSION_LOCK,
    lambda: scheduler_resource_reason(), lambda task: revalidate_queued_task(task), lambda task: execute_scheduled_task(task),
    note_activity=lambda: GPU_IDLE_RELEASER.note_activity(),
    global_limit=int(os.environ.get('DIRECTOR_QUEUE_GLOBAL_LIMIT', '256')),
    user_limit=int(os.environ.get('DIRECTOR_QUEUE_USER_LIMIT', '64')),
    cpu_workers=int(os.environ.get('DIRECTOR_CPU_WORKERS', '2')))


@app.get('/api/queue')
def queue_status():
    principal = current_principal.get()
    if principal is None or principal.administrator:
        return TASK_SCHEDULER.summary()
    with DB_LOCK, db() as connection:
        rows = connection.execute('SELECT * FROM tasks ORDER BY created_at DESC').fetchall()
    own = [task_row(row) for row in rows if json.loads(row['payload']).get('owner_user') == principal.identity.username]
    return {'gpu_slots': 1, 'cpu_slots': TASK_SCHEDULER.cpu_workers, 'global_limit': TASK_SCHEDULER.global_limit,
            'user_limit': TASK_SCHEDULER.user_limit,
            'tasks': [{'id': task['id'], 'status': task['status'], 'queue': TASK_SCHEDULER.queue_info(task['id'])} for task in own if task['status'] not in task_scheduler.TERMINAL]}


@app.post("/api/resources/release")
def release_resources() -> dict[str, Any]:
    """Compatibility endpoint; the UI relies on automatic idle release."""
    with GPU_ADMISSION_LOCK:
        try:
            reason = gpu_idle_reason()
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise HTTPException(503, f"无法确认 GPU 空闲状态: {exc}") from exc
        if reason:
            raise HTTPException(409, reason)
        try:
            request_comfy_free()
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise HTTPException(502, f"ComfyUI 回收请求失败: {exc}") from exc
        GPU_IDLE_RELEASER.note_manual_release()
    time.sleep(0.4)
    return {"released": True, "comfy": comfy_status(), "message": "已请求 ComfyUI 回收空闲模型和缓存"}


@app.post("/api/tasks/{task_id}/stop")
def stop(task_id: str) -> dict[str, Any]:
    task = get_current_project_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return request_task_stop(task)


@app.post("/api/projects/{project_id}/tasks/{task_id}/stop")
def stop_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    return request_task_stop(project_task_detail(project_id, task_id))


def request_task_stop(task: dict[str, Any]) -> dict[str, Any]:
    if TASK_SCHEDULER.cancel_pending(task['id']):
        if task['payload'].get('kind') == 'reference_semantic':
            background_jobs.update(analysis_job_root(), task['payload'], status='stopped')
        return get_task(task['id']) or task
    if task['payload'].get('kind') == 'media_operation':
        with MEDIA_OPERATION_LOCK:
            task = get_task(task['id']) or task
            if task['status'] == 'needs_reconcile':
                raise HTTPException(409, '请先核对媒体执行回执，不能把未知执行标记为已停止')
            if task['status'] not in {'succeeded', 'failed', 'stopped'}:
                set_task(task['id'], status='stop_requested', stop_requested=1,
                         error='正在请求停止媒体处理子进程，完成后才标记停止')
            return get_task(task['id']) or task
    if task['payload'].get('kind') == 'audio_edit':
        with AUDIO_EDIT_LOCK:
            task = get_task(task['id']) or task
            if task['status'] == 'needs_reconcile':
                raise HTTPException(409, '请先核对CPU音频编辑回执，不能把未知执行标记为已停止')
            if task['status'] not in {'succeeded', 'failed', 'stopped'}:
                set_task(task['id'], status='stop_requested', stop_requested=1,
                         error='已请求停止；CPU处理无法中途取消，结束后不登记候选')
            return get_task(task['id']) or task
    task_id = task['id']
    if task['status'] == 'needs_reconcile':
        raise HTTPException(409, '原任务待核对，请先核对原回执；不能把未知任务标记为已停止')
    if task["status"] in {"succeeded", "failed", "stopped"}:
        return task
    with DB_LOCK, db() as connection:
        connection.execute("UPDATE tasks SET stop_requested=1,status='stop_requested',updated_at=? WHERE id=? AND status NOT IN ('succeeded','failed','stopped','needs_reconcile')", (time.time(), task_id))
        connection.commit()
    return get_task(task_id) or task


class MissingExecutionResolution(BaseModel):
    confirm_execution_ended: Literal[True]
    note: str = Field(min_length=1, max_length=2000)


@app.post("/api/projects/{project_id}/tasks/{task_id}/resolve-missing")
@gpu_admission
def resolve_missing_execution(project_id: str, task_id: str, request: MissingExecutionResolution) -> dict[str, Any]:
    """Record an operator's lost-execution conclusion without submitting a retry."""
    task = project_task_detail(project_id, task_id)
    if task['payload'].get('kind') in {'reference_semantic', 'reference_decomposition'}:
        raise HTTPException(409, {'code': 'external_execution_unresolved', 'message': '参考进程不属于 ComfyUI；必须核对独立完成回执，不能依据 ComfyUI 空队列解除占用'})
    note = request.note.strip()
    if not note:
        raise HTTPException(422, '请填写原执行已结束及输出检查的核对说明')
    if task['payload'].get('kind') == 'media_operation':
        with MEDIA_OPERATION_LOCK:
            task = project_task_detail(project_id, task_id)
            if task['status'] != 'needs_reconcile':
                raise HTTPException(409, '仅支持待核对媒体任务')
            if audio_edit_owner_alive(task['payload']):
                raise HTTPException(409, '原执行服务仍存活，不能确认旧执行结束')
            try:
                worker_alive = media_operations.owner_alive(task['payload'])
            except (OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(409, f'无法确认媒体子进程结束: {exc}') from exc
            if worker_alive:
                raise HTTPException(409, '媒体子进程仍存活，不能释放执行名额')
            try:
                media_operations.read_receipt(task['payload'])
            except (OSError, ValueError, media_operations.MediaOperationFailed):
                pass
            else:
                raise HTTPException(409, '已有有效媒体回执，请核对原回执恢复结果')
            result = dict(task.get('result') or {})
            result['missing_execution_resolution'] = {'confirmed_at': time.time(), 'note': note,
                'execution_owner': task['payload'].get('execution_owner'), 'execution_verified_ended': True,
                'source': 'operator_confirmation', 'kind': 'media_operation'}
            set_task(task_id, status='failed', stop_requested=0, result=json.dumps(result, ensure_ascii=False),
                     error='原媒体执行已结束且无可恢复回执；已确认，未重新执行')
            return get_task(task_id)
    if task['payload'].get('kind') == 'audio_edit':
        with AUDIO_EDIT_LOCK:
            task = project_task_detail(project_id, task_id)
            if task['status'] != 'needs_reconcile':
                raise HTTPException(409, '仅支持待核对CPU音频编辑任务')
            if audio_edit_owner_alive(task['payload']):
                raise HTTPException(409, '原CPU执行服务仍存活，不能确认旧执行结束')
            try:
                audio_edit_tasks.read_receipt(task['payload'])
            except (OSError, ValueError):
                pass
            else:
                raise HTTPException(409, '已有有效CPU回执，请核对原回执恢复结果')
            result = dict(task.get('result') or {})
            result['missing_execution_resolution'] = {'confirmed_at': time.time(), 'note': note,
                'execution_owner': task['payload'].get('execution_owner'), 'execution_verified_ended': True,
                'source': 'operator_confirmation', 'kind': 'audio_edit'}
            set_task(task_id, status='failed', stop_requested=0, result=json.dumps(result, ensure_ascii=False),
                     error='CPU执行已结束且无可恢复回执；已人工确认，未重新执行')
            return get_task(task_id)
    if task['status'] != 'needs_reconcile' or task['payload'].get('kind') == 'speech':
        raise HTTPException(409, '仅支持缺失 ComfyUI 历史的待核对任务；声音任务请核对独立执行器')
    try:
        queue = request_json('/queue')
        history = request_json(f"/history/{task['prompt_id']}") if task.get('prompt_id') else {}
    except (OSError, ValueError, HTTPError, URLError) as exc:
        raise HTTPException(503, '无法核实原执行状态，请恢复连接后重试') from exc
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise HTTPException(503, '队列回执无效，不能结束核对')
    if queue['queue_running'] or queue['queue_pending']:
        raise HTTPException(409, '队列仍有任务，不能确认原执行已结束')
    if not isinstance(history, dict) or history:
        raise HTTPException(409, '原历史存在或回执无效，请使用核对原回执恢复结果')
    result = dict(task.get('result') or {})
    result['missing_execution_resolution'] = {
        'confirmed_at': time.time(), 'note': note, 'prompt_id': task.get('prompt_id'),
        'queue_verified_empty': True, 'history_verified_missing': True,
        'source': 'operator_confirmation',
    }
    with DB_LOCK, db() as connection:
        changed = connection.execute(
            "UPDATE tasks SET status='failed',stop_requested=0,error=?,result=?,updated_at=? WHERE id=? AND project_id=? AND status='needs_reconcile'",
            ('原执行历史缺失；已人工确认执行结束，未自动重跑', json.dumps(result, ensure_ascii=False), time.time(), task_id, project_id),
        ).rowcount
        connection.commit()
    if not changed:
        raise HTTPException(409, '任务状态已变化，请刷新后检查')
    return get_task(task_id) or task


@app.post("/api/projects/{project_id}/tasks/{task_id}/reconcile")
@gpu_admission
def reconcile_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Only reconcile the original execution; never create a retry."""
    task = project_task_detail(project_id, task_id)
    if task['status'] == 'succeeded':
        return task
    if task['status'] not in {'needs_reconcile', 'failed', 'stopped'}:
        raise HTTPException(409, '任务仍受执行器监控，请查询状态而非重复核对')
    return reconcile_task(task_in_project_context(task, require_project_manifest(project_id)))


def reconcile_task(task: dict[str, Any]) -> dict[str, Any]:
    if task['payload'].get('kind') == 'reference_semantic':
        reconcile_semantic_job(task['project_id'], task['payload']['analysis_id'])
        return get_task(task['id'])
    if task['payload'].get('kind') == 'reference_decomposition':
        payload = task['payload']
        try:
            receipt = json.loads(Path(payload['receipt_path']).read_text(encoding='utf-8'))
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(409, {'code': 'reference_receipt_unknown', 'message': '参考拆分完成回执尚未核实；不会重新执行'}) from exc
        expected = {'task_id': task['id'], 'source': str(Path(payload['source']).resolve()), 'analysis_path': str(Path(payload['analysis_path']).resolve()), 'status': 'succeeded'}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise HTTPException(409, {'code': 'reference_receipt_mismatch', 'message': '参考完成回执与冻结输入不匹配'})
        document, _ = read_reverse_analysis(task['project_id'], payload['analysis_id'])
        set_task(task['id'], status='succeeded', error=None, result=json.dumps(document, ensure_ascii=False))
        TASK_SCHEDULER.wake()
        return get_task(task['id'])
    """Recover a restart receipt without assuming an unknown submission failed."""
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_task(task)
    if task['payload'].get('kind') == 'media_operation':
        if task['status'] == 'stopped':
            return task
        try:
            receipt = media_operations.read_receipt(task['payload'])
        except (OSError, ValueError, media_operations.MediaOperationFailed) as exc:
            raise HTTPException(409, f'媒体回执尚未核实，不会重新执行: {exc}') from exc
        return complete_media_operation_task(task, receipt)
    if task['payload'].get('kind') == 'audio_edit':
        if task['status'] == 'stopped':
            return task
        try:
            receipt = audio_edit_tasks.read_receipt(task['payload'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'CPU音频编辑回执尚未核实，不会重新执行: {exc}') from exc
        return complete_audio_edit_task(task, receipt)
    if task['payload'].get('kind') == 'speech':
        try:
            receipt = speech.read_receipt(task['payload'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'声音完成回执尚未核实，不会重新生成: {exc}') from exc
        return complete_speech_task(task, receipt)
    prompt_id = task.get("prompt_id")
    if not prompt_id:
        raise HTTPException(409, "该任务没有提交回执，无法确认是否已生成；已阻止重复提交")
    try:
        queue = request_json("/queue")
    except (OSError, ValueError, HTTPError, URLError) as exc:
        raise HTTPException(503, "暂时无法核对 ComfyUI 队列，请恢复连接后重试") from exc
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ("queue_running", "queue_pending")):
        raise HTTPException(503, "ComfyUI 队列回执无效，尚未恢复任务")
    for item in queue["queue_running"] + queue["queue_pending"]:
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            raise HTTPException(409, "原任务仍在 ComfyUI 队列中；请等待完成后再次核对，不会重复生成")
    if task['payload'].get('kind') == 'keyframe':
        try:
            done = keyframes.read_result(request_json(f'/history/{prompt_id}'), str(prompt_id), task['payload'], OUTPUT_ROOT)
        except Exception as exc:
            raise HTTPException(409, f'关键帧结果尚未核实，不会重新生成: {exc}') from exc
        if not done:
            raise HTTPException(409, '尚未找到关键帧完成回执，不会重新生成')
        if done['status'] == 'error':
            set_task(task['id'], status='failed', result=json.dumps(done, ensure_ascii=False), error=comfy_execution_error(task['payload'], done))
            return get_task(task['id']) or task
        return complete_keyframe_task(task, done)
    done = history_done(str(prompt_id))
    if not done:
        raise HTTPException(409, "尚未找到原任务的完成记录，可能仍在执行或历史已清理；已阻止重复提交")
    done = attach_h3_duration_result(done, task['payload'])
    if done["status"] == "error":
        set_task(task["id"], status="failed", result=json.dumps(done, ensure_ascii=False), error=comfy_execution_error(task["payload"], done))
        return get_task(task["id"]) or task
    outputs = done.get("outputs", [])
    if not outputs:
        raise HTTPException(409, "原任务报告成功，但没有可恢复的视频输出；不会重新生成")
    for output in outputs:
        path = (OUTPUT_ROOT / str(output)).resolve()
        if not path.is_relative_to(OUTPUT_ROOT.resolve()) or not path.is_file():
            raise HTTPException(409, "原任务报告成功，但输出文件缺失或路径无效；请先核对输出文件")
    payload = task["payload"]
    target_plan = Path(str(payload.get("plan_path"))) if payload.get("plan_path") else PLAN_PATH
    done = publish_task_outputs(task, done)
    with PLAN_EDIT_LOCK:
        if target_plan.is_file():
            document = json.loads(target_plan.read_text(encoding="utf-8-sig"))
            is_assembly = task["asset_id"] == payload.get("assembly_asset_id", CURRENT_PROJECT.get("assembly_asset_id"))
            target = document.get("assembly", {}) if is_assembly else next((item for item in document.get("segments", []) if item.get("id") == task["asset_id"]), {})
            recorded_prompt = target.get("prompt_id" if is_assembly else "comfyui_task_id")
            previous_candidate = payload.get('previous_assembly_candidate')
            replaces_previous = (is_assembly and isinstance(previous_candidate, dict)
                                 and previous_candidate == {key: target.get(key) for key in ('prompt_id', 'output')})
            if recorded_prompt and recorded_prompt != prompt_id and not replaces_previous:
                raise HTTPException(409, "该片段已有其他生成版本；保留现有版本，请先人工核对历史结果")
            if not recorded_prompt and not replaces_previous and (target.get("output") if is_assembly else (target.get("video") or {}).get("path")):
                raise HTTPException(409, "该片段已有未关联回执的候选；保留现有候选，请先人工核对")
            version_recorded = (not is_assembly and any(
                isinstance(entry, dict) and entry.get("task_id") == task["id"]
                for entry in target.get("generation_history", [])
            ))
            if (not recorded_prompt or (replaces_previous and recorded_prompt != prompt_id)
                    or (not is_assembly and recorded_prompt == prompt_id and not version_recorded)):
                recorder = record_assembly_result if is_assembly else record_segment_result
                workflow = str(payload.get("workflow", ""))
                if is_assembly:
                    recorder(str(prompt_id), workflow, done, target_plan)
                else:
                    recorder(task["asset_id"], str(prompt_id), workflow, done, target_plan,
                             task_id=task["id"], payload=payload)
        set_task(task["id"], status="succeeded", stop_requested=0, error=None, result=json.dumps(done, ensure_ascii=False))
    return get_task(task["id"]) or task


@app.post("/api/tasks/{task_id}/resume")
@gpu_admission
def resume(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task and task.get("project_id") != CURRENT_PROJECT_ID:
        raise HTTPException(409, "该任务属于另一个项目，禁止在当前项目恢复")
    return resume_saved_task(task)


@app.post("/api/projects/{project_id}/tasks/{task_id}/resume")
@gpu_admission
def resume_project_task(project_id: str, task_id: str) -> dict[str, Any]:
    task = project_task_detail(project_id, task_id)
    manifest = require_project_manifest(project_id)
    return resume_saved_task(task_in_project_context(task, manifest))


def task_in_project_context(task: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    if PRIVATE_WORKSPACES is not None:
        PRIVATE_WORKSPACES.verify_task(task)
    project_id = str(manifest['id'])
    payload = dict(task['payload'])
    if payload.get('project_id', project_id) != project_id:
        raise HTTPException(409, '冻结任务与项目归属不一致，不能恢复')
    payload.setdefault('project_id', project_id)
    payload.setdefault('plan_path', str(resolve_registered_path(str(manifest['plan_path']))))
    payload.setdefault('assembly_asset_id', manifest.get('assembly_asset_id', ''))
    return {**task, 'payload': payload}


def resume_saved_task(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task or task["status"] not in {"stopped", "failed", "needs_reconcile"}:
        raise HTTPException(409, "只有已停止、失败或待核对任务可以恢复")
    if task['payload'].get('kind') in {'speech', 'keyframe', 'audio_edit', 'media_operation',
                                      'reference_decomposition', 'reference_semantic'}:
        return reconcile_task(task)
    if task["status"] == "needs_reconcile" or task.get("prompt_id"):
        task = reconcile_task(task)
        if task["status"] == "succeeded":
            return task
    require_submission_capacity()
    payload = task["payload"]
    new_id = str(uuid.uuid4())
    now = time.time()
    with DB_LOCK, db() as connection:
        check_queue_limits(connection, settings_owner())
        connection.execute("INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id, task["asset_id"], "scheduler_waiting", now, now, json.dumps(payload, ensure_ascii=False), task["project_id"]))
        connection.commit()
    queue_task(new_id)
    return get_task(new_id) or {"id": new_id, "status": "preparing"}


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str) -> StreamingResponse:
    require_project_manifest(project_id)
    async def stream():
        while True:
            status_data, task_data = await asyncio.gather(asyncio.to_thread(status), asyncio.to_thread(project_task_list, project_id, 100, 0))
            yield f"data: {json.dumps({'status': status_data, 'tasks': task_data['tasks'], 'project_id': project_id}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        while True:
            status_data, task_data = await asyncio.gather(asyncio.to_thread(status), asyncio.to_thread(tasks))
            yield f"data: {json.dumps({'status': status_data, 'tasks': task_data}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/media/{asset_name}")
def media(asset_name: str) -> FileResponse:
    path = MEDIA.get(asset_name)
    if path is None or not path.is_file():
        raise HTTPException(404, "素材不存在")
    return FileResponse(path)


@app.get("/media-file")
def media_file(path: str) -> FileResponse:
    """Serve an authorized asset from configured storage roots."""
    try:
        candidate = resolve_registered_path(path)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not candidate.is_file():
        raise HTTPException(404, "素材不存在")
    return FileResponse(candidate)


@app.get("/workflow-file")
def workflow_file(path: str) -> FileResponse:
    """Download a project-owned ComfyUI JSON graph for browser review/import."""
    raw = str(path).replace("\\", "/").strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        prefix = "ComfyUI-Workspace/projects/director-workbench/"
        relative = raw[len(prefix):] if raw.startswith(prefix) else raw
        candidate = PROJECT / relative
    candidate = registered_candidate(candidate)
    if PRIVATE_WORKSPACES is not None and current_principal.get() is not None:
        candidate = PRIVATE_WORKSPACES.file(candidate)
        if candidate.suffix.lower() != '.json':
            raise HTTPException(415, '这里只允许下载 JSON 工作流')
        if not candidate.is_file():
            raise HTTPException(404, '工作流不存在')
        return FileResponse(candidate, media_type='application/json', filename=candidate.name)
    try:
        candidate = candidate.resolve()
        workflow_roots = [PROJECT.resolve(), DIRECTOR_WORKSPACES_ROOT.resolve()]
        workflow_roots.extend(destination for source, destination in legacy_mappings(ROOT) if source == ROOT / 'ComfyUI-Workspace/projects/director-workbench/workspaces')
        if not any(candidate.is_relative_to(root) for root in workflow_roots):
            raise ValueError('Workflow outside configured project roots')
    except ValueError as exc:
        raise HTTPException(403, "工作流路径不在导演台项目目录内") from exc
    if candidate.suffix.lower() != ".json":
        raise HTTPException(415, "这里只允许下载 JSON 工作流")
    if not candidate.is_file():
        raise HTTPException(404, "工作流不存在")
    return FileResponse(candidate, media_type="application/json", filename=candidate.name)


class WorkbenchStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        if path in {'.', '', 'index.html'} or path.endswith('.html') or response.headers.get('content-type', '').startswith('text/html'):
            # Revalidate the entry document so a rebuild can select its new hashed assets.
            response.headers['Cache-Control'] = 'no-cache'
        return response


class AgentPageRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    label: str = Field(default='当前工作台',max_length=100)
    enabled: StrictBool = False


class AgentPageTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str | None = Field(default=None,max_length=160)
    asset_id: str | None = Field(default=None,max_length=160)
    task_id: str | None = Field(default=None,max_length=160)
    revision: StrictInt | None = Field(default=None,ge=0)


class AgentPageValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str | None = Field(default=None,max_length=100000)
    duration_seconds: float | None = Field(default=None,gt=0,le=600)
    generation: ShotGenerationRequest | None = None
    page: Literal['shots','script','creative','delivery'] | None = None


class AgentPageActionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page_id: str | None = Field(default=None,pattern=r'^[A-Za-z0-9_-]{16,80}$')
    action_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    kind: Literal['show_draft','locate','navigate','open_candidate','play','pause','show_preflight','show_task']
    target: AgentPageTarget = Field(default_factory=AgentPageTarget)
    values: AgentPageValues = Field(default_factory=AgentPageValues)


class AgentPageAck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['presented','not_presented','takeover']
    message: str = Field(default='',max_length=500)


AGENT_PAGE_STORES = {}


def agent_page_store():
    from .agent_pages import PageStore
    principal=current_principal.get()
    if principal is None: raise HTTPException(401,'请登录本人账号')
    root=RUNTIME/'agent-pages'
    with PLAN_EDIT_LOCK:
        store=AGENT_PAGE_STORES.setdefault(str(root),PageStore(root))
    return store,principal.identity.username


@app.post('/api/agent/pages')
def register_agent_page(request:AgentPageRegistration,http_request:BrowserRequest):
    if http_request.headers.get('authorization'): raise HTTPException(403,'页面登记只接受当前同源浏览器会话')
    store,owner=agent_page_store()
    return store.register(owner,request.page_id,request.label,request.enabled,http_request.headers.get('x-agent-page-key'))


@app.get('/api/agent/pages')
def list_agent_pages():
    store,owner=agent_page_store();return store.list(owner)


@app.get('/api/agent/pages/{page_id}/actions')
def read_agent_page_actions(page_id:str,request:BrowserRequest,after:int=Query(default=0,ge=0)):
    store,owner=agent_page_store()
    if request.headers.get('x-agent-page-key'): store.heartbeat(owner,page_id,request.headers['x-agent-page-key'])
    return store.read(owner,page_id,after)


@app.post('/api/agent/page-actions')
@plan_transaction
def create_agent_page_action(request:AgentPageActionRequest):
    store,owner=agent_page_store()
    target=request.target.model_dump(exclude_none=True)
    values=request.values.model_dump(exclude_none=True)
    previous=store.replay(owner,request.page_id,request.action_id,request.kind,target,values)
    if previous is not None: return previous
    if target.get('project_id'):
        require_project_manifest(target['project_id'])
        document,_=load_project_plan(target['project_id'])
        check_plan_revision(document,request.target.revision)
        if target.get('task_id'):
            task=project_task_detail(target['project_id'],target['task_id'])
            if target.get('asset_id') and task.get('asset_id')!=target['asset_id']: raise HTTPException(404,'任务不是该目标分镜')
        if request.kind!='show_draft' and target.get('asset_id') and not any(row.get('id')==target['asset_id'] for row in document['segments']):
            raise HTTPException(404,'目标分镜不存在')
    if request.kind=='show_draft' and request.values.duration_seconds is not None:
        try: h3_duration.plan(request.values.duration_seconds,(values.get('generation') or {}).get('aspect_ratio','16:9'))
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    return store.create(owner,request.page_id,request.action_id,request.kind,target,values)


@app.post('/api/agent/pages/{page_id}/actions/{action_id}/ack')
def acknowledge_agent_page_action(page_id:str,action_id:str,ack:AgentPageAck,request:BrowserRequest):
    if request.headers.get('authorization'): raise HTTPException(403,'实际呈现仅由同源发起标签页确认')
    store,owner=agent_page_store()
    return store.ack(owner,page_id,action_id,request.headers.get('x-agent-page-key'),ack.status,ack.message)


from . import agent_access
AGENT_REMOTE_MCP = agent_access.install(app,http_app_provider=lambda:app,
    token_provider=lambda: current_principal.get().token if current_principal.get() is not None else None)


if DIST.is_dir():
    # This mount is last so that /api and /media remain explicit backend routes.
    app.mount("/", WorkbenchStaticFiles(directory=DIST, html=True), name="director-ui")
