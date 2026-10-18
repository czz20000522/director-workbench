"""作品导入导出：工程目录打包导入与作品导出。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException
from ..schemas import ProjectImportRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.post("/api/projects/import")
def import_project(request: ProjectImportRequest) -> dict[str, Any]:
    backend.ensure_no_release_blocking_tasks("导入项目")
    raw_path = Path(request.path)
    source = raw_path if raw_path.is_absolute() else backend.ROOT / raw_path
    try:
        source = source.resolve()
        source.relative_to(backend.ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(403, f"只能导入 {backend.ROOT} 工作区内的目录") from exc
    if not source.is_dir():
        raise HTTPException(404, "导入目录不存在")
    project_id = request.project_id or source.name
    project_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in project_id).strip("-")[:80] or "imported-project"
    catalog = backend.load_project_catalog()
    existing = {str(item.get("id")) for item in catalog.get("projects", []) if isinstance(item, dict)}
    if project_id in existing:
        suffix = 2
        base = project_id
        while f"{base}-{suffix}" in existing:
            suffix += 1
        project_id = f"{base}-{suffix}"
    try:
        manifest, report = backend.scan_import_directory(source, project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        if manifest.get("plan_path"):
            backend.resolve_registered_path(str(manifest["plan_path"]))
        for registered in (manifest.get("media") or {}).values():
            backend.resolve_registered_path(str(registered))
        assembly = manifest.get("assembly") or {}
        if assembly.get("audio"):
            backend.resolve_registered_path(str(assembly["audio"]))
    except ValueError as exc:
        raise HTTPException(422, f"项目清单包含工作区外路径: {exc}") from exc
    manifest_path = backend.PROJECTS_ROOT / f"{project_id}.json"
    backend.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog.setdefault("projects", []).append({"id": project_id, "manifest": manifest_path.name})
    catalog["default_project_id"] = project_id
    backend.PROJECT_CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backend.apply_project_manifest(manifest)
    return {"project": backend.public_project(), "report": report, "manifest_path": backend.root_relative_path(manifest_path)}


@ROUTER.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    value = backend.public_project(manifest)
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    files: list[str] = []
    for path in media.values():
        try:
            if backend.resolve_registered_path(str(path)).is_file():
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
            loaded_plan, _ = backend.load_project_plan(project_id)
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
                workflow_path = backend.resolve_registered_path(reference)
            except ValueError:
                # Manifest helper references are project-relative (for
                # example tools/build_assembly_workflow.py), while media and
                # segment recipes may be shared-root-relative or absolute.
                candidate = Path(reference)
                workflow_path = (backend.PROJECT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                if not (workflow_path == backend.PROJECT.resolve() or backend.PROJECT.resolve() in workflow_path.parents):
                    raise
            record["path"] = backend.root_relative_path(workflow_path)
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
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
        if planned_asset_ids or assembly_asset_id:
            fallback_rows = connection.execute("SELECT * FROM tasks WHERE project_id IS NULL AND asset_id IN ({}) ORDER BY created_at".format(",".join("?" for _ in (planned_asset_ids | {assembly_asset_id}))), tuple(planned_asset_ids | {assembly_asset_id})).fetchall()
            rows = list(rows) + [row for row in fallback_rows if row["id"] not in {item["id"] for item in rows}]
    task_records = [backend.task_row(row) for row in rows]
    export_state = backend.build_project_state(project_id)
    candidate_manifest = []
    for segment in (plan_document or {}).get('segments', []):
        if not isinstance(segment, dict):
            continue
        video = segment.get('video') or {}
        finish = video.get('finish_review') or {}
        review = export_state.get('reviews', {}).get(str(segment.get('id')), {})
        candidate_manifest.append({
            'asset_id': segment.get('id'), 'prompt': segment.get('prompt'),
            'A': {'path': video.get('path'), 'task_id': segment.get('current_version_task_id') or segment.get('comfyui_task_id'), 'review': review.get('sample')},
            'B': {'path': finish.get('path'), 'task_id': finish.get('task_id') or finish.get('prompt_id'), 'processing': finish.get('processing'), 'review': review.get('finish')},
        })

    # Keep the export useful when handing one work to the next episode in the
    # same series.  Reuse the authenticated project catalog so private exports
    # never reveal works from another account.
    visible_projects = backend.projects().get('projects', [])
    series_name = str(manifest.get('series') or '')
    series_context = {
        'name': series_name,
        'current_project_id': project_id,
        'works': [
            {'id': item.get('id'), 'title': item.get('title'), 'selected': item.get('id') == project_id}
            for item in visible_projects
            if isinstance(item, dict) and str(item.get('series') or '') == series_name
        ],
        'handoff': {
            'last_segment_id': candidate_manifest[-1].get('asset_id') if candidate_manifest else None,
            'last_prompt': candidate_manifest[-1].get('prompt') if candidate_manifest else None,
            'adopted_candidates': [
                item['asset_id'] for item in candidate_manifest
                if isinstance(item.get('A'), dict) and isinstance(item['A'].get('review'), dict)
                and item['A']['review'].get('status') == 'approved'
            ],
        },
    }

    return {
        "schema_version": 2,
        "export_type": "director-workbench-project-bundle",
        "project": value,
        "plan": {"path": str(plan_path).replace("\\", "/") if plan_path else None, "content": plan_document},
        "workflow_files": workflow_files,
        "task_records": task_records,
        "project_state": export_state,
        "candidate_manifest": candidate_manifest,
        "series_context": series_context,
        "resource_manifest": {"files": files, "count": len(files)},
        "workspace_spec": "docs/workspace-spec.md",
    }
