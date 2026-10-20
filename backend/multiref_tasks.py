"""Workbench task entry for the explicit H3 multi-reference recipe.

This module turns one explicit project request into the existing task-table
record that the normal scheduler/runner already understands:

* project assets are checked for ownership/kind and copied to the ComfyUI input
  directory under a controlled, per-task path; an optional first-frame asset
  selects the direct-first-frame + timed-reference mode, while scene/character
  assets select the existing still-conditioned mode;
* :mod:`backend.multiref_recipe` builds and validates the API graph against the
  local ComfyUI object_info;
* the frozen graph is written to the project snapshot root, and the task is
  inserted with ``kind='workflow'`` so the existing ``run_workflow_submission``
  submits it, tracks history and calls ``record_segment_result`` on success;
* failure/unknown receipts are left to the existing runner, which already maps
  them to failed/needs_reconcile.

It never adopts a candidate, assembles a film, downloads models or starts the
ComfyUI/workbench service. The HTTP router is intentionally not wired into
``app.py`` here; the Lead owns that integration.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import multiref_recipe as recipe

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_PREFIX = 'director-workbench/multiref'


class MultiRefTaskError(RuntimeError):
    """Structured task-entry failure; HTTP converts it to a 4xx/5xx detail."""

    def __init__(self, code: str, message: str, status_code: int = 422, *,
                 blockers: list[dict[str, Any]] | None = None, **details: Any):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.blockers = list(blockers or [])
        self.details = details

    @property
    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'code': self.code,
            'message': str(self),
            'retryable': self.status_code in {409, 502, 503},
            **self.details,
        }
        if self.blockers:
            payload['blockers'] = self.blockers
        return payload


class MultiRefTaskRequest(BaseModel):
    """Explicit multi-reference task; no asset is selected automatically."""

    model_config = ConfigDict(extra='forbid')
    segment_id: str = Field(min_length=1, max_length=160)
    reference_asset_ids: list[str]
    scene_asset_id: str | None = Field(default=None, max_length=160)
    first_frame_asset_id: str | None = Field(default=None, max_length=160)
    prompt: str
    duration_seconds: float
    aspect_ratio: str
    seed: int = Field(default=42, ge=0, le=2147483647)
    still_steps: int = Field(default=8, ge=1, le=100)
    video_steps: int = Field(default=8, ge=1, le=100)
    preview: bool = True


def _backend():
    from . import app as backend
    return backend


def _asset_file(backend: Any, manifest: dict[str, Any], asset_id: str, *, role: str) -> tuple[dict[str, Any], Path]:
    assets = manifest.get('assets') if isinstance(manifest.get('assets'), list) else []
    asset = next((item for item in assets
                  if isinstance(item, dict) and str(item.get('id')) == asset_id), None)
    if asset is None:
        raise MultiRefTaskError(
            'reference_asset_not_owned',
            f'{role}素材 {asset_id} 不属于当前作品。',
            403,
            blockers=[{'code': 'reference_asset_not_owned', 'asset_id': asset_id, 'role': role}],
        )
    if asset.get('kind') != 'image':
        raise MultiRefTaskError(
            'reference_asset_not_image',
            f'{role}素材 {asset_id} 不是图片素材。',
            422,
            blockers=[{'code': 'reference_asset_not_image', 'asset_id': asset_id, 'role': role,
                       'kind': asset.get('kind')}],
        )
    source_value = asset.get('image_path') or (asset.get('sources') or {}).get('A')
    if not source_value:
        raise MultiRefTaskError(
            'reference_asset_unavailable',
            f'{role}素材 {asset_id} 缺少已登记文件。',
            409,
            blockers=[{'code': 'reference_asset_unavailable', 'asset_id': asset_id, 'role': role}],
        )
    try:
        source = backend.resolve_registered_path(str(source_value))
    except (OSError, ValueError, RuntimeError) as exc:
        raise MultiRefTaskError(
            'reference_asset_unavailable',
            f'{role}素材 {asset_id} 文件无法解析: {exc}',
            409,
            blockers=[{'code': 'reference_asset_unavailable', 'asset_id': asset_id, 'role': role}],
        ) from exc
    if not source.is_file():
        raise MultiRefTaskError(
            'reference_asset_unavailable',
            f'{role}素材 {asset_id} 文件不存在: {source}',
            409,
            blockers=[{'code': 'reference_asset_unavailable', 'asset_id': asset_id,
                       'role': role, 'path': str(source)}],
        )
    if source.suffix.lower() not in recipe.IMAGE_SUFFIXES:
        raise MultiRefTaskError(
            'reference_asset_not_image',
            f'{role}素材 {asset_id} 的扩展名不是支持的图片格式。',
            422,
            blockers=[{'code': 'reference_asset_not_image', 'asset_id': asset_id,
                       'role': role, 'suffix': source.suffix}],
        )
    return asset, source


def _copy_asset(source: Path, destination: Path) -> dict[str, Any]:
    """Copy one project asset into a controlled ComfyUI input file."""
    try:
        before = source.stat()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        after = source.stat()
        copied = destination.stat()
    except OSError as exc:
        raise MultiRefTaskError(
            'asset_copy_failed', f'复制素材到 ComfyUI input 失败: {exc}', 502,
            blockers=[{'code': 'asset_copy_failed', 'source': str(source),
                       'message': str(exc)}],
        ) from exc
    if ((after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)
            or copied.st_size != before.st_size):
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise MultiRefTaskError(
            'asset_changed', '素材在复制期间发生变化，请重新读取后重试。', 409,
            blockers=[{'code': 'asset_changed', 'source': str(source)}],
        )
    return {'source': str(source), 'path': str(destination),
            'size_bytes': copied.st_size, 'mtime_ns': copied.st_mtime_ns}


def _stage_inputs(backend: Any, task_id: str,
                  references: list[tuple[dict[str, Any], Path]],
                  scene: tuple[dict[str, Any], Path] | None,
                  first_frame: tuple[dict[str, Any], Path] | None) -> tuple[dict[str, str], list[dict[str, Any]], list[Path]]:
    """Copy explicit assets in caller order and return controlled LoadImage names.

    Files are flat basenames under the ComfyUI input root so LoadImage can
    resolve them while ``multiref_recipe`` still sees simple image names.
    """
    input_root = Path(backend.INPUT_ROOT)
    prefix = f'dsh-multiref-{task_id}'
    names: dict[str, str] = {}
    staged: list[dict[str, Any]] = []
    written: list[Path] = []
    entries: list[tuple[str, Path]] = []
    if first_frame is not None:
        entries.append(('first-frame', first_frame[1]))
    elif scene is not None:
        entries.append(('primary', scene[1]))
    elif references:
        entries.append(('primary', references[0][1]))
    for index, (_, source) in enumerate(references, start=1):
        if first_frame is None and scene is None and index == 1:
            continue
        entries.append((f'ref-{index:02d}', source))
    for label, source in entries:
        suffix = source.suffix.lower()
        destination = input_root / f'{prefix}-{label}{suffix}'
        copied = _copy_asset(source, destination)
        names[label] = destination.name
        written.append(destination)
        staged.append({'label': label, 'reference': destination.name,
                       'source': copied['source'], 'path': copied['path'],
                       'size_bytes': copied['size_bytes'], 'mtime_ns': copied['mtime_ns']})
    return names, staged, written


def _connect_staged_materials(graph: dict[str, Any], staged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_reference = {item['reference']: item for item in staged}
    bound: list[dict[str, Any]] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or node.get('class_type') != 'LoadImage':
            continue
        reference = (node.get('inputs') or {}).get('image')
        item = by_reference.get(reference)
        if item is None:
            raise MultiRefTaskError(
                'staged_material_missing',
                f'工作流节点 {node_id} 的图片没有对应的受控暂存文件。',
                500,
                blockers=[{'code': 'staged_material_missing', 'node_id': node_id,
                           'reference': reference}],
            )
        bound.append({
            'node_id': node_id,
            'input': 'image',
            'reference': reference,
            'path': item['path'],
            'source': item['source'],
            'size_bytes': item['size_bytes'],
        })
    return bound


def _cleanup_staging(written: list[Path] | None) -> None:
    for path in written or []:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def create_task(project_id: str, request: MultiRefTaskRequest, *,
                object_info: dict[str, Any] | None = None,
                base_url: str = recipe.COMFY_URL) -> dict[str, Any]:
    """Create one queued multi-reference workflow task from explicit inputs."""
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    document, plan_path = backend.load_project_plan(project_id)
    segment = next((item for item in document.get('segments', [])
                    if isinstance(item, dict) and str(item.get('id')) == request.segment_id), None)
    if segment is None:
        raise MultiRefTaskError(
            'segment_not_found', f'目标分镜不存在: {request.segment_id}', 404,
            blockers=[{'code': 'segment_not_found', 'segment_id': request.segment_id}],
        )
    references = [_asset_file(backend, manifest, asset_id, role='参考图')
                  for asset_id in request.reference_asset_ids]
    if request.first_frame_asset_id is not None and request.scene_asset_id is not None:
        raise MultiRefTaskError(
            'first_frame_scene_exclusive',
            '首帧素材与场景图不能同时提供；请选择直接首帧或多参考首帧其中一种模式。',
            422,
            blockers=[{'code': 'first_frame_scene_exclusive',
                       'fields': ['first_frame_asset_id', 'scene_asset_id']}],
        )
    if request.first_frame_asset_id is not None:
        first_frame = _asset_file(backend, manifest, request.first_frame_asset_id, role='首帧图')
        if len(references) > recipe.MAX_REF_IMAGES:
            raise MultiRefTaskError(
                'too_many_reference_images',
                f'直接首帧模式下最多 {recipe.MAX_REF_IMAGES} 张语义参考图。',
                422,
                blockers=[{'code': 'too_many_reference_images',
                           'value': len(references), 'max': recipe.MAX_REF_IMAGES}],
            )
    else:
        first_frame = None
        if not references:
            raise MultiRefTaskError(
                'reference_images_required',
                '没有首帧时至少需要一张当前作品的图片素材作为角色参考。',
                422,
                blockers=[{'code': 'reference_images_required',
                           'field': 'reference_asset_ids'}],
            )
    if request.scene_asset_id is not None:
        scene = _asset_file(backend, manifest, request.scene_asset_id, role='场景图')
    else:
        scene = None
    if first_frame is None and scene is None and len(references) > recipe.MAX_PICTURES:
        raise MultiRefTaskError(
            'too_many_reference_images',
            f'没有场景图时最多 {recipe.MAX_PICTURES} 张角色参考图。',
            422,
            blockers=[{'code': 'too_many_reference_images',
                       'value': len(references), 'max': recipe.MAX_PICTURES}],
        )
    if first_frame is None and scene is not None and len(references) > recipe.MAX_REF_IMAGES:
        raise MultiRefTaskError(
            'too_many_reference_images',
            f'有场景图时最多 {recipe.MAX_REF_IMAGES} 张角色参考图。',
            422,
            blockers=[{'code': 'too_many_reference_images',
                       'value': len(references), 'max': recipe.MAX_REF_IMAGES}],
        )
    if not isinstance(request.prompt, str) or not request.prompt.strip():
        raise MultiRefTaskError('prompt_required', '分镜 prompt 不能为空。', 422)
    if not recipe.MIN_DURATION <= float(request.duration_seconds) <= recipe.MAX_DURATION:
        raise MultiRefTaskError(
            'invalid_duration',
            f'H3 时长必须在 {recipe.MIN_DURATION:g}–{recipe.MAX_DURATION:g} 秒之间。',
            422,
        )
    if request.aspect_ratio not in recipe.ASPECT_SIZES:
        raise MultiRefTaskError(
            'invalid_aspect_ratio', '画幅必须是 16:9、9:16 或 1:1。', 422,
            options=list(recipe.ASPECT_SIZES),
        )
    backend.ensure_asset_not_pending(project_id, request.segment_id)
    task_id = str(uuid.uuid4())
    staged_files: list[Path] | None = None
    workflow_path: Path | None = None
    created = False
    try:
        names, staged, staged_files = _stage_inputs(backend, task_id, references, scene, first_frame)
        if first_frame is not None or scene is not None:
            character_names = [names[f'ref-{index:02d}']
                               for index in range(1, len(references) + 1)]
        else:
            # Without a scene the recipe uses the first character reference as
            # Picture 1; pass the complete ordered character list, not only the
            # later ref_images slots.
            character_names = [names['primary']] + [
                names[f'ref-{index:02d}'] for index in range(2, len(references) + 1)
            ]
        recipe_request = recipe.MultiRefRequest(
            character_ref_images=character_names,
            first_frame_image=names.get('first-frame') if first_frame is not None else None,
            scene_image=names.get('primary') if scene is not None else None,
            prompt=request.prompt,
            duration_seconds=request.duration_seconds,
            aspect_ratio=request.aspect_ratio,
            seed=request.seed,
            still_steps=request.still_steps,
            video_steps=request.video_steps,
            preview=request.preview,
            output_prefix=f'导演工作台工作目录/{project_id}/{request.segment_id}/multiref-{task_id}',
        )
        prepared = recipe.prepare(
            recipe_request,
            object_info=object_info,
            base_url=base_url,
        )
        graph = prepared['prompt']
        metadata = prepared['metadata']
        staged_materials = _connect_staged_materials(graph, staged)
        snapshot_root = backend.task_snapshot_root(project_id)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        workflow_path = snapshot_root / f'multiref-{task_id}.json'
        backend.write_json_atomic(workflow_path, graph)
        execution_snapshot = {
            'api_graph': str(workflow_path),
            'prompt': metadata.get('video_prompt') or request.prompt,
            'original_prompt': request.prompt,
            'parameters': {
                'duration_seconds': request.duration_seconds,
                'aspect_ratio': request.aspect_ratio,
                'seed': request.seed,
                'frames': metadata.get('frames'),
                'actual_duration_seconds': metadata.get('actual_duration_seconds'),
            },
            'materials': {},
            'staged_materials': staged_materials,
            'multiref': {
                'schema_version': SCHEMA_VERSION,
                'recipe': recipe.RECIPE_ID,
                'mode': metadata.get('mode'),
                'reference_asset_ids': list(request.reference_asset_ids),
                'scene_asset_id': request.scene_asset_id,
                'first_frame_asset_id': request.first_frame_asset_id,
                'first_frame_image': metadata.get('first_frame_image'),
                'reference_prompt_tags': metadata.get('reference_prompt_tags') or [],
                'controlled_images': names,
                'prepare': {
                    'environment_ready': bool(prepared.get('environment', {}).get('ready')),
                    'reference_order': metadata.get('reference_order'),
                },
            },
            'frozen_at': time.time(),
        }
        payload: dict[str, Any] = {
            'kind': 'workflow',
            'project_id': project_id,
            'asset_id': request.segment_id,
            'plan_path': str(plan_path),
            'assembly_asset_id': str(manifest.get('assembly_asset_id') or 'MASTER'),
            'workflow': str(workflow_path),
            'source_workflow': str(workflow_path),
            'execution_snapshot': execution_snapshot,
            'seed': request.seed,
            'prompt': request.prompt,
            'multiref': {
                **execution_snapshot['multiref'],
                'duration_seconds': request.duration_seconds,
                'aspect_ratio': request.aspect_ratio,
                'seed': request.seed,
            },
            'segment_dependencies': list(segment.get('dependencies') or []),
        }
        payload['segment_snapshot'] = backend.segment_generation_snapshot(segment, payload)
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
        now = time.time()
        with backend.DB_LOCK, backend.db() as connection:
            backend.check_queue_limits(connection, backend.settings_owner())
            connection.execute(
                'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,stop_requested,payload,project_id) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (task_id, request.segment_id, 'scheduler_waiting', now, now, 0,
                 json.dumps(payload, ensure_ascii=False), project_id),
            )
            connection.commit()
            created = True
        backend.queue_task(task_id)
        return backend.get_task(task_id) or {
            'id': task_id, 'project_id': project_id, 'asset_id': request.segment_id,
            'status': 'scheduler_waiting',
        }
    except BaseException:
        if not created:
            _cleanup_staging(staged_files)
            if workflow_path is not None:
                try:
                    workflow_path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise


def get_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Return the existing task shape for one created multi-reference task."""
    backend = _backend()
    task = backend.project_task_detail(project_id, task_id)
    payload = task.get('payload') if isinstance(task.get('payload'), dict) else {}
    if payload.get('kind') != 'workflow' or not isinstance(payload.get('multiref'), dict):
        raise MultiRefTaskError('multiref_task_not_found',
                                f'该作品中不存在多参考图任务: {task_id}', 404)
    return task
