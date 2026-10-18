"""素材库：Windows 输入/输出/下载与当前工程目录的浏览、登记与复用。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shutil
import time
from typing import Any
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from ..schemas import MaterialRegisterRequest, MaterialReuseRequest
from .. import app as backend


ROUTER = APIRouter()


def asset_record_paths(asset: dict[str, Any]) -> list[Any]:
    paths: list[Any] = []
    sources = asset.get("sources")
    if isinstance(sources, dict):
        paths.extend(sources.values())
    if asset.get("image_path"):
        paths.append(asset["image_path"])
    return paths


def material_roots(project_id: str) -> dict[str, tuple[str, Path, str]]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get("workspace_root")
    workspace = backend.resolve_registered_path(str(workspace_value)) if workspace_value else (backend.DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    if backend.PRIVATE_WORKSPACES is not None and backend.current_principal.get() is not None:
        return {'project': ('当前项目', workspace, 'copy')}
    roots = {
        "input": ("Windows 输入素材", backend.INPUT_ROOT.resolve(), "reference"),
        "output": ("Windows 生成结果", backend.OUTPUT_ROOT.resolve(), "reference"),
        "project": ("当前项目", workspace.resolve(), "reference"),
    }
    downloads = backend.windows_downloads_root()
    if downloads:
        roots["downloads"] = ("Windows 下载目录", downloads, "copy")
    return roots


def resolve_material_path(project_id: str, source_id: str, relative_path: str = "") -> tuple[str, Path, Path, str]:
    roots = backend.material_roots(project_id)
    if source_id not in roots:
        raise HTTPException(422, "未知素材库")
    label, root, import_mode = roots[source_id]
    relative = PurePosixPath(relative_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(422, "非法素材路径")
    candidate = root / Path(*relative.parts)
    target = backend.PRIVATE_WORKSPACES.file(candidate) if backend.PRIVATE_WORKSPACES is not None and backend.current_principal.get() is not None else candidate.resolve()
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
        "kind": "directory" if is_directory else backend.material_kind(item),
        "size_bytes": 0 if is_directory else stat.st_size,
        "modified_at": stat.st_mtime,
        "download_url": None if is_directory else relative,
    }


@ROUTER.get("/api/material-library")
def browse_material_library(
    project_id: str,
    source_id: str = "output",
    path: str = "",
    query: str = "",
    kind: str = "all",
) -> dict[str, Any]:
    roots = backend.material_roots(project_id)
    label, root, directory, _ = backend.resolve_material_path(project_id, source_id, path)
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
        entry_kind = "directory" if item.is_dir() else backend.material_kind(item)
        if not item.is_dir() and entry_kind == "other":
            continue
        if kind != "all" and not item.is_dir() and entry_kind != kind:
            continue
        try:
            entries.append(backend.public_material_entry(item, root))
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


@ROUTER.get("/material-file")
def download_material_file(project_id: str, source_id: str, path: str) -> FileResponse:
    _, _, target, _ = backend.resolve_material_path(project_id, source_id, path)
    if not target.is_file() or backend.material_kind(target) == "other":
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(target, filename=target.name)


@ROUTER.post("/api/projects/{project_id}/materials/register")
def register_server_materials(project_id: str, request: MaterialRegisterRequest) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    media = manifest.get("media") if isinstance(manifest.get("media"), dict) else {}
    assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    workspace_value = manifest.get("workspace_root")
    workspace = backend.resolve_registered_path(str(workspace_value)) if workspace_value else (backend.DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    known_asset_ids = {str(item.get("id")) for item in assets if isinstance(item, dict)}
    registered: list[dict[str, Any]] = []
    for requested in request.items:
        source_label, _, target, import_mode = backend.resolve_material_path(project_id, requested.source_id, requested.relative_path)
        kind = backend.material_kind(target)
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
        root_relative = backend.root_relative_path(registered_target)
        existing = next(
            (item for item in assets if isinstance(item, dict) and root_relative in backend.asset_record_paths(item)),
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
        record = backend.imported_asset_records({alias: root_relative})[0]
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
    backend.save_project_manifest(manifest)
    if project_id == backend.CURRENT_PROJECT_ID:
        backend.apply_project_manifest(manifest)
    return {"project": backend.public_project(manifest), "registered": registered}


@ROUTER.get("/api/projects/{project_id}/reusable-materials")
def reusable_materials(project_id: str) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    materials: dict[str, dict[str, Any]] = {}
    diagnostic_paths = set()
    for asset in manifest.get('assets', []):
        if isinstance(asset, dict) and (asset.get('operation') == 'video_qc' or asset.get('purpose') == 'diagnostic'):
            for value in backend.asset_record_paths(asset):
                try:
                    diagnostic_paths.add(str(backend.resolve_registered_path(value)).casefold())
                except (ValueError, OSError):
                    continue

    def add(value: Any, title: str) -> None:
        if not isinstance(value, str) or not value:
            return
        try:
            path = backend.resolve_registered_path(value)
            if str(path).casefold() in diagnostic_paths:
                return
            kind = backend.material_kind(path)
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
            for path in backend.asset_record_paths(asset):
                add(path, str(asset.get("title") or asset.get("id") or "项目素材"))
    for title, path in (manifest.get("media") or {}).items():
        add(path, title)
    try:
        plan = json.loads(backend.resolve_registered_path(manifest["plan_path"]).read_text(encoding="utf-8"))
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


@ROUTER.post("/api/projects/{project_id}/materials/reuse")
def reuse_project_materials(project_id: str, request: MaterialReuseRequest) -> dict[str, Any]:
    with backend.MATERIAL_REUSE_LOCK:
        if project_id == request.source_project_id:
            raise HTTPException(422, "请选择其他作品的素材")
        try:
            manifest = backend.load_project_manifest(project_id)
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc
        available = {item["id"]: item for item in backend.reusable_materials(request.source_project_id)["materials"]}
        requested = list(dict.fromkeys(request.material_ids))
        if any(item not in available for item in requested):
            raise HTTPException(409, "来源素材已变化或不存在，请刷新后重新选择")
        assets = list(manifest.get("assets") or [])
        registered = []
        already_registered = 0
        workspace = backend.resolve_registered_path(str(manifest["workspace_root"])) if manifest.get("workspace_root") else (backend.DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
        for material_id in requested:
            source = available[material_id]
            provenance = {"project_id": request.source_project_id, "material_id": material_id}
            if any(isinstance(asset, dict) and all((asset.get("reused_from") or {}).get(key) == value for key, value in provenance.items()) and backend._asset_record_exists(asset) for asset in assets):
                already_registered += 1
                continue
            source_path = backend.resolve_registered_path(source["path"])
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
            relative = backend.root_relative_path(destination)
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
            backend.save_project_manifest(manifest)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
        return {"registered": registered, "already_registered": already_registered}
