"""计划与分镜：分镜段的创建、编辑、检查与计划快照。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
from fastapi import APIRouter, HTTPException
from .. import creative_inspection, h3_duration, production_presets
from ..schemas import PipelineStageCreateRequest, PlanSegmentCreateRequest, PlanSegmentOperationRequest, PlanSegmentUpdateRequest, SegmentDeleteRequest, ShotGenerationRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.post("/api/projects/{project_id}/pipeline/stages")
def create_pipeline_stage(project_id: str, request: PipelineStageCreateRequest) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(project_id)
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
        "input_specs": [{"id": f"input-{index}", "label": label, "kind": backend.pipeline_value_kind(label), "required": True, "description": f"提交“{request.title.strip()}”前需要准备的输入。", **backend.pipeline_control(label)} for index, label in enumerate(inputs, start=1)],
        "output_specs": [{"id": f"output-{index}", "label": label, "kind": backend.pipeline_value_kind(label), "description": f"“{request.title.strip()}”完成后登记的结果。", "state": "待接入"} for index, label in enumerate(outputs, start=1)],
        "execution": {"mode": mode, "label": request.workflow.strip() or request.backend.strip() or "待接入执行器", "references": [], "state": "待接入"},
    }
    stages.append(stage)
    manifest["pipeline"] = stages
    backend.save_project_manifest(manifest)
    if project_id == backend.CURRENT_PROJECT_ID:
        backend.apply_project_manifest(manifest)
    return {"project": backend.public_project(manifest), "stage": stage}


def record_segment_artifacts(project_id: str, segment: dict[str, Any], source: str = "plan-editor") -> None:
    segment_id = str(segment.get("id", ""))
    keyframes = segment.get("keyframes") if isinstance(segment.get("keyframes"), dict) else {}
    audio = segment.get("audio") if isinstance(segment.get("audio"), dict) else {}
    references = [str(value) for value in [keyframes.get("first"), keyframes.get("last"), audio.get("guide"), audio.get("delivery_master")] if value]
    backend.append_project_record(
        "artifact:scene",
        {"title": f"{segment_id} 场景", "content": {key: segment.get(key) for key in ("location", "shot_size", "camera", "wardrobe")}, "references": references[:2], "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )
    backend.append_project_record(
        "artifact:performance",
        {"title": f"{segment_id} 动作表演", "content": {"performance": segment.get("performance"), "audio": audio}, "references": references[2:], "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )
    backend.append_project_record(
        "artifact:storyboard",
        {"title": f"{segment_id} 分镜", "content": {key: segment.get(key) for key in ("start_seconds", "end_seconds", "duration_seconds", "prompt", "workflow", "dependencies")}, "references": references, "status": "pending_review"},
        asset_id=segment_id, source=source, project_id=project_id,
    )


@ROUTER.post("/api/projects/{project_id}/plan/segments")
@backend.plan_transaction
def create_plan_segment(project_id: str, request: PlanSegmentCreateRequest) -> dict[str, Any]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    principal = backend.current_principal.get()
    if principal and not principal.administrator and request.workflow:
        if request.workflow != (manifest.get('production_preset') or {}).get('workflow'):
            raise HTTPException(403, '工作流由管理员维护，请选择已配置的制作方案')
    plan_value = manifest.get("plan_path")
    if not plan_value:
        raise HTTPException(422, "项目没有可写入的计划路径")
    try:
        plan_path = backend.resolve_registered_path(str(plan_value))
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
    backend.check_plan_revision(document, request.expected_revision)
    if request.generation is not None:
        backend.validate_creative_prompt(request.prompt, request.generation.creative_profile)
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
        segment.update(backend.validate_script_link(project_id, request.script_scene_id.strip(), request.expected_script_revision))
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
    backend.resolve_automatic_shot(manifest, segment)
    backend.validate_h3_plan_duration(manifest, segment)
    segments.append(segment)
    document["segments"] = segments
    backend.persist_plan_edit(project_id, document, plan_path)
    backend.record_segment_artifacts(project_id, segment, "director-ui")
    return {"project": backend.public_project(manifest), "segment": segment, "plan": document}


def load_project_plan(project_id: str) -> tuple[dict[str, Any], Path]:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        plan_path = backend.resolve_registered_path(str(manifest.get("plan_path", "")))
        document = json.loads(plan_path.read_text(encoding="utf-8-sig")) if plan_path.is_file() else {"schema_version": 1, "project": project_id, "status": "draft", "segments": [], "assembly": None}
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"项目计划无法读取: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        raise HTTPException(422, "项目计划缺少 segments 数组")
    # Older finish receipts predate media-fact persistence. Enrich the public
    # in-memory plan from the already registered B file so history/export can
    # show its measured dimensions without rewriting the user's plan on GET.
    for segment in document.get('segments', []):
        if not isinstance(segment, dict):
            continue
        finish = (segment.get('video') or {}).get('finish_review')
        if not isinstance(finish, dict):
            continue
        processing = finish.get('processing')
        if not isinstance(processing, dict) or isinstance(processing.get('output_size'), dict):
            continue
        facts = backend.authorized_media_facts(finish.get('path'))
        if facts.get('known'):
            processing['output_size'] = {key: facts.get(key) for key in (
                'width', 'height', 'duration_seconds', 'fps', 'frames', 'audio_streams')}
            if not finish.get('specs') or '×' not in str(finish.get('specs')):
                finish['specs'] = f"AI 高清候选 · 2x · {facts.get('width')}×{facts.get('height')}"
    return document, plan_path


def authorized_media_facts(reference):
    if not reference:
        return {'known': False, 'reason': 'not_selected', 'duration_seconds': None}
    try:
        return creative_inspection.probe(backend.resolve_registered_path(str(reference)))
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
                'facts': backend.authorized_media_facts(path), 'source': 'registered_asset'}
    if segment.get('source_analysis'):
        try:
            analysis, _ = backend.read_reverse_analysis(manifest['id'], segment['source_analysis'])
            source = analysis.get('source') or {}
            return {'analysis_id': analysis['id'], 'title': source.get('original_name', analysis.get('title')),
                    'path': source.get('path'), 'facts': backend.authorized_media_facts(source.get('path')), 'source': 'analysis'}
        except HTTPException:
            return {'facts': {'known': False}, 'source': 'analysis_unavailable'}
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
                graph = json.loads(backend.resolve_workflow_path(str(segment['workflow'])).read_text(encoding='utf-8-sig'))
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
                profile = h3_duration.plan(segment.get('duration_seconds'), aspect, production_presets.megapixels(preset_id))
                profile['generation_mode'] = 'text_to_video' if preset_id in production_presets.TEXT_PRESET_IDS else 'image_to_video'
            except ValueError:
                profile = None
    video = (segment.get('video') or {}).get('path')
    observed = backend.authorized_media_facts(video)
    planned = ({'model': 'H3', 'width': profile['width'], 'height': profile['height'],
                'fps': profile['fps'], 'duration_seconds': profile['playback_seconds'],
                'frames': profile['frame_count']} if profile else {'model': 'unknown', 'duration_seconds': None})
    try:
        reference = backend.shot_reference(manifest, segment)
    except HTTPException:
        reference = {'facts': {'known': False}, 'source': 'registered_asset_unavailable'}
    assembly = backend.assembly_trim_summary(manifest, document)
    trim = next((row for row in (assembly or {}).get('segments', []) if row['segment_id'] == segment['id']), None)
    return {'revision': document.get('revision', 0), 'segment_id': segment['id'],
            'candidate_version': segment.get('current_version_task_id'),
            'reference_options': [{'id': row['id'], 'title': row.get('title', row['id'])} for row in manifest.get('assets', []) if row.get('kind') == 'video'],
            'reference_comparison': creative_inspection.comparison(reference, planned,
                 audio_policy=((document.get('assembly_edit') or {}).get('sound') or {}).get('policy', 'legacy_master' if (manifest.get('assembly') or {}).get('audio') else 'segment_native'),
                  first_frame=backend.authorized_media_facts(None if (profile or {}).get('generation_mode') == 'text_to_video' else (profile or {}).get('first_frame') or (segment.get('keyframes') or {}).get('first'))),
            'timing': {'requested_seconds': (profile or {}).get('requested_seconds', segment.get('duration_seconds')), 'expected_candidate': profile,
                       'observed_candidate': observed, 'assembly_take': trim,
                       'observed_assembly': backend.authorized_media_facts((document.get('assembly') or {}).get('output')),
                       'note': '请求是剧情计划；模型帧网格决定预计候选；实际媒体须测量；装配按取片范围独立对齐。'}}


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
        preset_id = next((key for key in production_presets.PRESET_IDS
                          if selected.get('id') == production_presets.stage_id(key)), None)
        h3_duration.plan(segment.get('duration_seconds'), production_presets.aspect_ratio(preset_id) if preset_id else '1:1',
                         production_presets.megapixels(preset_id) if preset_id else 0.4)
    except ValueError as exc:
        raise HTTPException(422, f"分镜 {segment.get('id', '')} 时长无效：{exc}") from exc


def persist_plan_edit(project_id: str, document: dict[str, Any], plan_path: Path) -> None:
    backend.recalculate_segment_timeline(document["segments"])
    document["status"] = "draft"
    assembly = document.get("assembly")
    if isinstance(assembly, dict) and assembly.get("output"):
        note = "制作计划已变化，旧成片保留供查看，需要重新装配并审核"
        assembly.update(status="stale", notes=note)
        manifest = backend.load_project_manifest(project_id)
        asset_id = str(manifest.get("assembly_asset_id") or "MASTER")
        with backend.DB_LOCK, backend.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM project_records WHERE project_id=? AND asset_id=? AND record_type='review:final' ORDER BY revision DESC, id DESC LIMIT 1",
                (project_id, asset_id),
            ).fetchone()
            if row:
                review = json.loads(row["data"])
                if review.get("status") != "stale":
                    backend.append_project_record_in_connection(
                        connection, project_id, "review:final",
                        {**review, "status": "stale", "note": note},
                        asset_id, "plan-editor", time.time(),
                    )
            connection.commit()
    backend.write_plan_version(plan_path, document)


def mark_segment_checkpoints_stale(project_id: str, segment_id: str, note: str) -> None:
    for stage_id in ("scene", "performance", "storyboard", "materials", "generate", "review"):
        backend.append_project_record(
            f"checkpoint:{stage_id}",
            {"stage_id": stage_id, "status": "stale", "note": note, "upstream_revisions": {}},
            asset_id=segment_id,
            source="plan-editor",
            project_id=project_id,
        )
    state = backend.build_project_state(project_id)
    for stage in ("sample", "finish"):
        existing = state.get("reviews", {}).get(segment_id, {}).get(stage)
        if existing:
            backend.append_project_record(
                f"review:{stage}",
                {"stage": stage, "status": "stale", "note": note, "adopted_variant": existing.get("adopted_variant"), "candidate_ref": existing.get("candidate_ref")},
                asset_id=segment_id,
                source="upstream-change",
                project_id=project_id,
            )


@ROUTER.delete('/api/projects/{project_id}/plan/segments/{segment_id}')
@backend.plan_transaction
def delete_plan_segment(project_id: str, segment_id: str, request: SegmentDeleteRequest) -> dict[str, Any]:
    if request.confirm != segment_id:
        raise HTTPException(422, '请填写完整分镜编号以确认删除')
    backend.ensure_no_release_blocking_tasks('删除分镜')
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_revision)
    before = document['segments']
    after = [item for item in before if not isinstance(item, dict) or str(item.get('id')) != segment_id]
    if len(after) == len(before):
        raise HTTPException(404, '分镜不存在')
    for item in after:
        if isinstance(item, dict) and isinstance(item.get('dependencies'), list):
            item['dependencies'] = [value for value in item['dependencies'] if value != segment_id]
    document['segments'] = after
    backend.persist_plan_edit(project_id, document, plan_path)
    return {'deleted_segment_id': segment_id, 'revision': document['revision'], 'plan': document}


@ROUTER.patch("/api/projects/{project_id}/plan/segments/{segment_id}")
@backend.plan_transaction
def update_plan_segment(project_id: str, segment_id: str, request: PlanSegmentUpdateRequest) -> dict[str, Any]:
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_revision)
    segment = next((item for item in document["segments"] if isinstance(item, dict) and item.get("id") == segment_id), None)
    if segment is None:
        raise HTTPException(404, "分镜不存在")
    principal = backend.current_principal.get()
    if principal and not principal.administrator and request.workflow is not None and request.workflow != segment.get('workflow', ''):
        raise HTTPException(403, '工作流由管理员维护，请调整制作方案提供的参数')
    if request.reference_asset_id:
        backend.shot_reference(backend.require_project_manifest(project_id), {'reference_asset_id': request.reference_asset_id})
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
        updates.update(backend.validate_script_link(project_id, request.script_scene_id.strip(), request.expected_script_revision))
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
    backend.validate_creative_prompt(str(segment.get('prompt') or ''), str((segment.get('generation') or {}).get('creative_profile', 'general')))
    backend.resolve_automatic_shot(backend.require_project_manifest(project_id), segment)
    if request.duration_seconds is not None or request.workflow is not None:
        backend.validate_h3_plan_duration(backend.require_project_manifest(project_id), segment)
    backend.persist_plan_edit(project_id, document, plan_path)
    backend.record_segment_artifacts(project_id, segment)
    backend.mark_segment_checkpoints_stale(project_id, segment_id, "分镜内容已修改，需要重新确认受影响阶段")
    return {"segment": segment, "plan": document, "project": backend.public_project(backend.require_project_manifest(project_id))}


@ROUTER.post("/api/projects/{project_id}/plan/segments/{segment_id}/operate")
@backend.plan_transaction
def operate_plan_segment(project_id: str, segment_id: str, request: PlanSegmentOperationRequest) -> dict[str, Any]:
    document, plan_path = backend.load_project_plan(project_id)
    backend.check_plan_revision(document, request.expected_revision)
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
        manifest = backend.require_project_manifest(project_id)
        duration_ids = ({copied['id']} if request.operation == 'copy' else
                        {segment_id, second['id']} if request.operation == 'split' else {first['id']})
        for item in segments:
            if item.get('id') in duration_ids:
                backend.validate_h3_plan_duration(manifest, item)
    backend.persist_plan_edit(project_id, document, plan_path)
    for item in segments:
        if item.get("id") in affected_ids:
            backend.record_segment_artifacts(project_id, item)
    for affected_id in sorted(affected_ids):
        backend.mark_segment_checkpoints_stale(project_id, affected_id, f"分镜执行了 {request.operation} 操作")
    return {"plan": document, "segments": segments}
