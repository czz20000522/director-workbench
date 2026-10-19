"""工作台状态与校验：健康/状态、计划与流水线契约、项目就绪校验。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
import copy
import threading
import time
from typing import Any
from fastapi import APIRouter, HTTPException
import math
import shlex
from .. import production_presets, readiness, shot_creation
from ..schemas import PipelineValidateRequest
from .. import app as backend


ROUTER = APIRouter()
_READINESS_CACHE_LOCK = threading.RLock()
_READINESS_CACHE: dict[tuple[str, str], tuple[tuple[Any, ...], dict[str, Any]]] = {}


def validate_pipeline_values(stage: dict[str, Any], values: dict[str, Any], manifest: dict[str, Any] | None = None, *, collect_errors: bool = False) -> dict[str, Any]:
    contract = backend.pipeline_contract(stage, manifest)
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
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute("UPDATE tasks SET status = 'needs_reconcile', error = '服务重启后需与 ComfyUI 历史核对', updated_at = ? WHERE status IN ('submitting','queued','running','stopping')", (time.time(),))
        rows = connection.execute("SELECT id,payload FROM tasks WHERE status IN ('preparing','submitting','queued','running','stopping','stop_requested','needs_reconcile')").fetchall()
        for row in rows:
            if json.loads(row['payload']).get('kind') in {'audio_edit', 'audio_master', 'media_operation'}:
                connection.execute("UPDATE tasks SET status='needs_reconcile',error='服务重启后需核对CPU媒体处理回执；不会自动重执行',updated_at=? WHERE id=?", (time.time(), row['id']))
        connection.commit()


def start_workbench() -> None:
    backend.mark_orphans()
    backend.TASK_SCHEDULER.start()
    backend.GPU_IDLE_RELEASER.start()


def stop_gpu_idle_release() -> None:
    backend.TASK_SCHEDULER.stop()
    backend.GPU_IDLE_RELEASER.stop()


@ROUTER.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "director-workbench", "comfy": backend.comfy_status()}


@ROUTER.get("/api/status")
def status() -> dict[str, Any]:
    """Engine status only: the runtime root is a machine path the browser never needs."""
    return {"comfy": backend.comfy_status()}


@ROUTER.get("/api/plan")
def plan() -> dict[str, Any]:
    """Return the latest generation plan without claiming it is a final cut."""
    if not backend.PLAN_PATH.is_file():
        return {"schema_version": 1, "project": backend.CURRENT_PROJECT_ID, "status": "empty", "segments": [], "assembly": None}
    try:
        return json.loads(backend.PLAN_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"无法读取作品分段计划: {exc}") from exc


@ROUTER.get("/api/pipeline")
def pipeline() -> dict[str, Any]:
    """Return the operator-facing stage contracts for the current work."""
    return {"schema_version": 2, "project": backend.CURRENT_PROJECT_ID, "stages": [backend.pipeline_contract(stage) for stage in backend.PIPELINE_STAGES]}


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
        declared = backend.load_pipeline_input_contracts().get(stage["id"]) or backend.PIPELINE_INPUT_CONTRACTS.get(stage["id"])
    else:
        contract_path = (backend.PROJECT / str(manifest.get('pipeline_contract', 'contracts/pipeline-inputs.json'))).resolve()
        if not contract_path.is_relative_to(backend.PROJECT.resolve()):
            raise HTTPException(422, '制作步骤契约路径越界')
        declared = backend.load_pipeline_input_contracts(contract_path).get(stage['id'])
    if isinstance(stage.get("input_specs"), list):
        value["input_specs"] = stage["input_specs"]
    elif declared and declared.get("mode") == "replace":
        value["input_specs"] = declared["inputs"]
    else:
        value["input_specs"] = [
            {"id": f"input-{index}", "label": label, "kind": backend.pipeline_value_kind(label),
             "required": True, "description": f"提交“{stage['title']}”前需要准备的输入.", **backend.pipeline_control(label)}
            for index, label in enumerate(stage.get("inputs", []), start=1)
        ] + backend.PIPELINE_INPUT_OVERRIDES.get(stage["id"], [])
    # Existing installed recipes keep their saved graph and shots. Only the
    # advertised contract is refreshed when the server gains duration support.
    preset_id = next((key for key in production_presets.PRESET_IDS
                      if stage.get('id') == production_presets.stage_id(key)
                      and stage.get('recipe', '').startswith('H3 ')), None)
    if preset_id:
        value['input_specs'] = production_presets.input_specs(preset_id)
        value['preset_id'] = preset_id
        value['aspect_ratio'] = production_presets.aspect_ratio(preset_id)
    output_state = "待接入" if stage.get("status") == "待接入" else "待生成"
    value["output_specs"] = stage["output_specs"] if isinstance(stage.get("output_specs"), list) else [
        {"id": f"output-{index}", "label": label, "kind": backend.pipeline_value_kind(label),
         "description": f"“{stage['title']}”完成后登记的结果。", "state": output_state}
        for index, label in enumerate(stage.get("outputs", []), start=1)
    ]
    mode = "comfyui" if stage.get("backend") == "ComfyUI 工作流" else "script" if stage.get("backend") == "本地脚本" else "hybrid"
    state = "已验证" if stage.get("status") == "已接入" else "待接入" if stage.get("status") == "待接入" else "可调试"
    value["execution"] = stage.get("execution") if isinstance(stage.get("execution"), dict) else {"mode": mode, "label": stage.get("backend", "未指定"), "references": [stage.get("recipe", "")], "state": state}
    return value


@ROUTER.get("/api/pipeline/{stage_id}")
def pipeline_detail(stage_id: str) -> dict[str, Any]:
    """Return one independently inspectable production-step contract."""
    stage = next((item for item in backend.PIPELINE_STAGES if item["id"] == stage_id), None)
    if stage is None:
        raise HTTPException(404, "制作步骤不存在")
    return {"schema_version": 2, "project": backend.CURRENT_PROJECT_ID, "stage": backend.pipeline_contract(stage)}


@ROUTER.post("/api/pipeline/{stage_id}/validate")
def validate_pipeline(stage_id: str, request: PipelineValidateRequest) -> dict[str, Any]:
    return backend.public_validate_pipeline(stage_id, request)


@ROUTER.get("/api/projects/{project_id}/pipeline")
def project_pipeline(project_id: str) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    return {"schema_version": 2, "project": project_id, "stages": [backend.pipeline_contract(stage, manifest) for stage in manifest.get('pipeline', [])]}


@ROUTER.post("/api/projects/{project_id}/pipeline/{stage_id}/validate")
def scoped_validate_pipeline(project_id: str, stage_id: str, request: PipelineValidateRequest) -> dict[str, Any]:
    return backend.public_validate_pipeline(stage_id, request, backend.require_project_manifest(project_id))


def public_validate_pipeline(stage_id, request, manifest=None):
    project = manifest or backend.CURRENT_PROJECT
    try:
        if stage_id == 'auto':
            raise HTTPException(422, {'code': 'explicit_stage_required',
                                      'message': '自动制作方案已移除；请显式选择制作步骤后再预检。'})
        return backend.validate_project_pipeline(stage_id, request, manifest)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {'code': 'validation_failed', 'message': str(exc.detail)}
        document, _ = backend.load_project_plan(str(project['id']))
        validation = {**detail, 'valid': False}
        if not validation.get('blockers'):
            validation['blockers'] = [{'code': detail['code'], 'message': detail['message'], 'action': 'edit_inputs'}]
        detail = {**detail, 'readiness': readiness.validation_readiness(validation, project_id=str(project['id']),
                    asset_id=request.asset_id or '', stage_id=stage_id, revision=document.get('revision', 0),
                    source='draft' if request.values else 'saved')}
        raise HTTPException(exc.status_code, detail) from exc


@backend.plan_transaction
def validate_project_pipeline(stage_id: str, request: PipelineValidateRequest, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    project = manifest if manifest is not None else backend.CURRENT_PROJECT
    stages = project.get('pipeline', []) if manifest is not None else backend.PIPELINE_STAGES
    templates = project.get('task_templates', {}) if manifest is not None else backend.TASK_TEMPLATES
    plan_path = backend.resolve_registered_path(str(project['plan_path'])) if manifest is not None else backend.PLAN_PATH
    stage = next((item for item in stages if item["id"] == stage_id), None)
    if stage is None:
        raise HTTPException(404, "制作步骤不存在")
    if not request.asset_id:
        return backend.validate_pipeline_values(stage, request.values, manifest)
    payload = dict(templates.get(request.asset_id, {}))
    payload["asset_id"] = request.asset_id
    try:
        document = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        document = {"segments": []}
    segment = next((item for item in document.get("segments", []) if isinstance(item, dict) and item.get("id") == request.asset_id), None)
    if isinstance(segment, dict) and segment.get("workflow") and not payload.get("workflow"):
        payload["workflow"] = str(segment["workflow"])
    if not payload.get('workflow'):
        payload['workflow'], _ = backend.select_preset_execution(project, None, stage_id)
    if not payload.get('workflow'):
        raise HTTPException(422, {
            'code': 'workflow_required',
            'message': f'{request.asset_id} 尚未明确绑定可执行工作流或制作方案。',
            'blockers': [{'code': 'workflow_required', 'object': {'kind': 'segment', 'id': request.asset_id},
                          'action': {'id': 'choose_workflow', 'label': '选择制作方案', 'method': 'PATCH',
                                     'path': f'/api/projects/{project["id"]}/plan/segments/{request.asset_id}'}}],
        })
    payload['workflow'], stage_id = backend.select_preset_execution(project, payload.get('workflow'), stage_id)
    prepared = backend.prepare_pipeline_payload(payload, stage_id, request.values, manifest)
    result = backend.validate_pipeline_values(stage, prepared["pipeline_values"], manifest)
    result["binding_count"] = len(prepared.get("_graph_bindings", []))
    result["materials"] = prepared.get("materials", {})
    result["parameters"] = prepared.get("parameters", {})
    if isinstance(prepared.get('h3_duration'), dict):
        result['h3_duration'] = prepared['h3_duration']
    if prepared.get('creative_inspection'):
        result['creative_inspection'] = prepared['creative_inspection']
    blockers = backend.segment_dependency_blockers(document, segment or {})
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
            exists = bool(reference and backend.resolve_registered_path(str(reference)).is_file())
        except (HTTPException, ValueError, OSError):
            exists = False
        if not exists:
            blockers.append({'code': 'waiting_dependency', 'message': f'等待前置分镜 {dependency} 的可读视频', 'dependency': str(dependency), 'action': 'inspect_dependency'})
    return blockers


def readiness_selection_options(manifest: dict[str, Any], stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return explicit selection facts only; nothing here chooses a recipe."""
    options = shot_creation.creation_options(manifest)
    installed = []
    for stage in stages:
        if not isinstance(stage, dict) or not stage.get('id'):
            continue
        references = (stage.get('execution') or {}).get('references') or []
        installed.append({'stage_id': str(stage['id']), 'title': stage.get('title', stage['id']),
                          'workflow': str(references[0]) if len(references) == 1 and references[0] else None})
    options['installed_pipeline_stages'] = installed
    return options


def project_readiness(project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    signature = backend.project_cache_signature(project_id)
    cache_key = (backend.project_cache_scope(), str(project_id))
    with _READINESS_CACHE_LOCK:
        cached = _READINESS_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return copy.deepcopy(cached[1])
    manifest = backend.require_project_manifest(project_id)
    document, _ = backend.load_project_plan(project_id)
    stages = manifest.get('pipeline', [])
    with backend.DB_LOCK, backend.db() as connection:
        raw_tasks = connection.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC', (project_id,)).fetchall()
    task_values = [backend.task_row(row) for row in raw_tasks]
    queue_values = backend.TASK_SCHEDULER.queue_info_many([task['id'] for task in task_values])
    for task in task_values:
        task['queue'] = queue_values.get(task['id'], {})
    def select_stage(segment, available):
        workflow = segment.get('workflow')
        if not workflow:
            return None
        _, selected = backend.select_preset_execution(manifest, workflow, None)
        if selected:
            return selected
        contracts = [backend.pipeline_contract(stage, manifest) for stage in available]
        executable = [stage for stage in contracts if (stage.get('execution') or {}).get('mode') == 'comfyui' and
                      any(item.get('kind') == '视频' for item in stage.get('output_specs', []))]
        matched = next((stage for stage in executable if workflow in (stage.get('execution') or {}).get('references', [])), None)
        return (matched or {}).get('id')
    result = readiness.build_project_readiness(project_id=project_id, plan=document, stages=stages, project_state=state,
        tasks=task_values, stage_selector=select_stage, selection_options=backend.readiness_selection_options(manifest, stages),
        assembly_asset_id=str(manifest.get('assembly_asset_id') or 'MASTER'),
        validate=lambda stage, asset, values: backend.validate_project_pipeline(stage, PipelineValidateRequest(asset_id=asset, values=values), manifest),
        assembly_preflight=lambda: backend.preflight_project_assembly(project_id))
    result['installed_stages'] = [{'id': stage['id'], 'title': stage.get('title', stage['id'])} for stage in stages]
    selected_stages = {row.get('stage_id') for row in result['segments'] if row.get('stage_id')}
    result['input_requirements'] = [spec for stage in stages if stage.get('id') in selected_stages
                                    for spec in backend.pipeline_contract(stage, manifest).get('input_specs', [])]
    with _READINESS_CACHE_LOCK:
        _READINESS_CACHE[cache_key] = (signature, copy.deepcopy(result))
    return result
