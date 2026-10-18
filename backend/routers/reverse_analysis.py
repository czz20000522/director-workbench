"""参考分析：拆解文档读写、模型状态、语义分析作业与转制作草案。

从 backend/app.py 拆出。沿用 private_mode 的既有做法：本模块只用
``backend.<名称>`` 在调用时读取工作台级对象，因此 backend/app.py 上既有的
monkeypatch（setattr(backend, "X", ...)）语义完全不变。
路由用 APIRouter 注册，由 app.py 在原先的位置 include_router，保持注册顺序。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sys
import time
import uuid
from fastapi import APIRouter, File, HTTPException, UploadFile
from .. import background_jobs
from ..schemas import ReverseArtifactUpdateRequest, ReverseClassificationRequest, ReverseConvertRequest, ReverseTimelineUpdateRequest
from .. import app as backend


ROUTER = APIRouter()


def reverse_analysis_root(project_id: str) -> Path:
    try:
        manifest = backend.load_project_manifest(project_id)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_value = manifest.get("workspace_root")
    workspace = backend.resolve_registered_path(str(workspace_value)) if workspace_value else (backend.DIRECTOR_WORKSPACES_ROOT / project_id).resolve()
    root = (workspace / "reference-analysis").resolve()
    if workspace != root and workspace not in root.parents:
        raise HTTPException(422, "参考拆解目录越界")
    root.mkdir(parents=True, exist_ok=True)
    return root


def reverse_analysis_path(project_id: str, analysis_id: str) -> Path:
    if not analysis_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in analysis_id):
        raise HTTPException(422, "非法参考拆解 ID")
    root = backend.reverse_analysis_root(project_id)
    target = (root / analysis_id).resolve()
    if root not in target.parents:
        raise HTTPException(422, "参考拆解路径越界")
    return target


def read_reverse_analysis(project_id: str, analysis_id: str) -> tuple[dict[str, Any], Path]:
    path = backend.reverse_analysis_path(project_id, analysis_id) / "analysis.json"
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


@ROUTER.get("/api/models/reverse-analysis")
def reverse_model_status() -> dict[str, Any]:
    return {
        "primary": {
            "id": "qwen3.8-27b-uncensored-q4-k-m",
            "label": "Qwen3.8-27B Uncensored Q4_K_M",
            "architecture": "native_multimodal_dense",
            "role": "vision_story_analysis_prompt_writing",
            "model_ready": backend.PRIMARY_MODEL.is_file(),
            "projector_ready": backend.PRIMARY_MMPROJ.is_file(),
            "ready": backend.PRIMARY_MODEL.is_file() and backend.PRIMARY_MMPROJ.is_file(),
            "model_size_bytes": backend.PRIMARY_MODEL.stat().st_size if backend.PRIMARY_MODEL.is_file() else 0,
            "projector_size_bytes": backend.PRIMARY_MMPROJ.stat().st_size if backend.PRIMARY_MMPROJ.is_file() else 0,
        },
        "fast_optional": {
            "id": "qwen3-vl-8b-instruct-q4-k-m",
            "label": "Qwen3-VL-8B-Instruct Q4_K_M",
            "role": "optional_fast_visual_prepass",
            "ready": backend.FAST_MODEL.is_file() and backend.FAST_MMPROJ.is_file(),
            "model_size_bytes": backend.FAST_MODEL.stat().st_size if backend.FAST_MODEL.is_file() else 0,
        },
    }


@ROUTER.get("/api/projects/{project_id}/reverse-analyses")
def list_reverse_analyses(project_id: str) -> dict[str, Any]:
    root = backend.reverse_analysis_root(project_id)
    analyses: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/analysis.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("project_id") == project_id:
            analyses.append(document)
    return {"analyses": analyses}


@ROUTER.get("/api/projects/{project_id}/reverse-analyses/{analysis_id}")
def get_reverse_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, _ = backend.read_reverse_analysis(project_id, analysis_id)
    with backend.DB_LOCK, backend.db() as connection:
        row = connection.execute('SELECT id FROM tasks WHERE project_id=? AND asset_id=? ORDER BY created_at DESC LIMIT 1',
                                 (project_id, f'reference-import-{analysis_id}')).fetchone()
    if row:
        task = backend.get_task(row['id'])
        document = {**document, 'import_task_id': task['id'], 'import_status': task['status'],
                    'import_task': {'id': task['id'], 'status': task['status'], 'queue': task['queue'], 'error': task.get('error')}}
    return document


@ROUTER.post("/api/projects/{project_id}/reverse-analyses", status_code=202)
async def create_reverse_analysis(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    backend.require_submission_capacity()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".flac", ".m4a", ".aac"}:
        raise HTTPException(422, "只支持常见音视频参考素材")
    analysis_id = f"ref-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    output = backend.reverse_analysis_path(project_id, analysis_id)
    source_dir = output / "source"
    source_dir.mkdir(parents=True, exist_ok=False)
    source = source_dir / f"reference{suffix}"
    await backend.stream_upload(file, source)
    command = [
        sys.executable, str(backend.REFERENCE_DECOMPOSER), "--project-id", project_id,
        "--analysis-id", analysis_id, "--source", str(source), "--output", str(output),
        "--original-name", file.filename or source.name,
    ]
    task_id = str(uuid.uuid4())
    receipt_path = output / 'import-completion.json'
    command.extend(['--task-id', task_id, '--completion-receipt', str(receipt_path)])
    document = {'id': analysis_id, 'project_id': project_id, 'title': file.filename or '参考素材',
                'source': {'original_name': file.filename or source.name, 'path': backend.root_relative_path(source), 'duration_seconds': 0, 'width': None, 'height': None},
                'stages': [{'id': 'materials', 'status': 'waiting'}], 'timeline': [], 'artifacts': {},
                'import_task_id': task_id, 'import_status': 'scheduler_waiting'}
    payload = {'kind': 'reference_decomposition', 'project_id': project_id, 'asset_id': f'reference-import-{analysis_id}',
               'analysis_id': analysis_id, 'source': str(source), 'analysis_path': str(output / 'analysis.json'), 'receipt_path': str(receipt_path), 'command': command}
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    with backend.GPU_ADMISSION_LOCK, backend.DB_LOCK, backend.db() as connection:
        backend.check_queue_limits(connection, backend.settings_owner())
        connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                           (task_id, payload['asset_id'], 'scheduler_waiting', time.time(), time.time(), json.dumps(payload, ensure_ascii=False), project_id))
        backend.write_reverse_analysis(document, output / 'analysis.json')
        connection.commit()
        backend.GPU_IDLE_RELEASER.note_activity()
    backend.TASK_SCHEDULER.wake()
    return backend.get_reverse_analysis(project_id, analysis_id)


@ROUTER.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/artifacts/{artifact_id}")
def update_reverse_artifact(project_id: str, analysis_id: str, artifact_id: str, request: ReverseArtifactUpdateRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
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
        backend.write_reverse_analysis(document, path)
        return {"artifact_id": artifact_id, "artifact": artifact, "analysis": document}


@ROUTER.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/classification")
def correct_reverse_classification(project_id: str, analysis_id: str, request: ReverseClassificationRequest) -> dict[str, Any]:
    reason = request.reason.strip()
    if not reason:
        raise HTTPException(422, '请填写内容类型校订依据')
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
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
        backend.write_reverse_analysis(document, path)
        return {'analysis': document}


@ROUTER.patch("/api/projects/{project_id}/reverse-analyses/{analysis_id}/timeline/{segment_id}")
def update_reverse_timeline(project_id: str, analysis_id: str, segment_id: str, request: ReverseTimelineUpdateRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        current_revision = int(document.get("revision", 0))
        if request.expected_revision is not None and request.expected_revision != current_revision:
            raise HTTPException(409, {"code": "analysis_revision_conflict", "message": "参考分析已更新，请比较最新内容后再保存；本次校订未覆盖服务器", "current_revision": current_revision, "expected_revision": request.expected_revision, "retryable": False})
        timeline = document.get("timeline") if isinstance(document.get("timeline"), list) else []
        segment = next((item for item in timeline if isinstance(item, dict) and item.get("id") == segment_id), None)
        if not segment:
            raise HTTPException(404, "参考时间段不存在")
        segment["user_note"] = request.user_note.strip()
        segment["status"] = request.status
        backend.write_reverse_analysis(document, path)
        return {"segment": segment, "analysis": document}


@ROUTER.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic", status_code=202)
def run_reverse_semantic_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    return backend.start_semantic_job(project_id, analysis_id)


def reserve_reverse_semantic(project_id: str, analysis_id: str) -> Path:
    _, path = backend.read_reverse_analysis(project_id, analysis_id)
    if not backend.PRIMARY_MODEL.is_file() or not backend.PRIMARY_MMPROJ.is_file():
        raise HTTPException(409, "27B 多模态主模型或视觉投影尚未就绪")
    with backend.GPU_ADMISSION_LOCK:
        backend.ensure_no_unresolved_analysis_jobs()
        if not backend.REVERSE_SEMANTIC_LOCK.acquire(blocking=False):
            raise HTTPException(409, "已有参考内容分析正在运行，请等待完成后再试")
        try:
            backend.ensure_no_release_blocking_tasks("开始参考内容分析", gpu_only=True, project_id=project_id)
            info = backend.comfy_status()
            if info.get("queue_running", 0) or info.get("queue_pending", 0):
                raise HTTPException(409, "ComfyUI 正在处理任务，请等待完成后再分析参考")
            backend.GPU_IDLE_RELEASER.note_activity()
        except Exception:
            backend.REVERSE_SEMANTIC_LOCK.release()
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
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        if document.get("semantic_result") != backend.root_relative_path(result_path):
            apply_result(document, result)
            document["semantic_result"] = backend.root_relative_path(result_path)
            backend.write_reverse_analysis(document, path)
    return document


def execute_reverse_semantic(project_id: str, analysis_id: str, path: Path, job: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if backend.PRIVATE_WORKSPACES is not None and job is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': project_id, 'payload': job})
        result_path = Path(job["result_path"]) if job else path.parent / "semantic-results" / f"{uuid.uuid4().hex}.json"
        receipt_args = ["--completion-receipt", job["receipt_path"], "--job-id", job["id"]] if job else []
        # A full 36-frame analysis may require nine visual batches plus synthesis.
        backend.run_reference_command(
            [sys.executable, str(backend.REFERENCE_SEMANTIC_ANALYZER), "--analysis", str(path), "--result-output", str(result_path), *receipt_args],
            timeout=10800, label="参考内容分析", **({'task_id': job['task_id']} if job and job.get('task_id') else {}),
        )
        if job and (backend.get_task(job.get('task_id', '')) or {}).get('stop_requested'):
            return backend.read_reverse_analysis(project_id, analysis_id)[0]
        return backend.merge_semantic_result(project_id, analysis_id, result_path)
    finally:
        backend.REVERSE_SEMANTIC_LOCK.release()


@ROUTER.get("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs/current")
def current_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, path = backend.read_reverse_analysis(project_id, analysis_id)
    with background_jobs.LOCK:
        job = next((job for job in background_jobs.records(backend.analysis_job_root()) if job["project_id"] == project_id and job["analysis_id"] == analysis_id), None)
    public_job = background_jobs.public(job)
    if job and job.get('task_id'):
        task = backend.get_task(job['task_id'])
        if task:
            public_job['status'] = 'queued' if task['status'] in {'scheduler_waiting', 'batch_waiting'} else task['status']
            public_job['queue'] = backend.TASK_SCHEDULER.queue_info(task['id'])
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


@ROUTER.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs", status_code=202)
def start_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, path = backend.read_reverse_analysis(project_id, analysis_id)
    root = backend.analysis_job_root()
    with backend.GPU_ADMISSION_LOCK, background_jobs.LOCK:
        previous = next((job for job in background_jobs.records(root) if job["project_id"] == project_id and job["analysis_id"] == analysis_id and job["status"] in background_jobs.ACTIVE), None)
        if previous and previous.get('task_id'):
            task = backend.get_task(previous['task_id'])
            if task and task['status'] in {'succeeded', 'failed', 'stopped'}:
                previous = None
        if previous:
            return backend.current_semantic_job(project_id, analysis_id)
        if document.get('import_task_id') and (backend.get_task(document['import_task_id']) or {}).get('status') != 'succeeded':
            raise HTTPException(422, {'code': 'reference_import_not_ready', 'message': '请先等待参考素材拆分完成，再开始内容分析', 'action': 'inspect_task'})
        if not backend.PRIMARY_MODEL.is_file() or not backend.PRIMARY_MMPROJ.is_file():
            raise HTTPException(409, {'code': 'model_unavailable', 'message': '参考语义分析模型尚未就绪'})
        backend.require_submission_capacity()
        now = time.time()
        with backend.DB_LOCK, backend.db() as connection:
            backend.check_queue_limits(connection, backend.settings_owner())
            job = background_jobs.create(root, project_id, analysis_id)
            if backend.PRIVATE_WORKSPACES is not None:
                backend.PRIVATE_WORKSPACES.freeze_owner(job, project_id)
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
        backend.TASK_SCHEDULER.wake()
        return backend.current_semantic_job(project_id, analysis_id)


@ROUTER.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/semantic-jobs/reconcile")
def reconcile_semantic_job(project_id: str, analysis_id: str) -> dict[str, Any]:
    _, analysis_path = backend.read_reverse_analysis(project_id, analysis_id)
    with backend.GPU_ADMISSION_LOCK, background_jobs.LOCK:
        job = next((item for item in background_jobs.records(backend.analysis_job_root()) if item['project_id'] == project_id and item['analysis_id'] == analysis_id), None)
        if not job:
            raise HTTPException(404, '没有可核对的分析任务')
        if backend.PRIVATE_WORKSPACES is not None:
            backend.PRIVATE_WORKSPACES.verify_task({'project_id': project_id, 'payload': job})
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
        backend.merge_semantic_result(project_id, analysis_id, result_path)
        job = background_jobs.update(backend.analysis_job_root(), job, status='succeeded', error=None, recovered_at=time.time())
        if job.get('task_id'):
            backend.set_task(job['task_id'], status='succeeded', error=None)
            backend.TASK_SCHEDULER.wake()
        return {'job': background_jobs.public(job)}


@ROUTER.post("/api/projects/{project_id}/reverse-analyses/{analysis_id}/convert")
@backend.plan_transaction
def convert_reverse_analysis(project_id: str, analysis_id: str, request: ReverseConvertRequest | None = None) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        if request is not None:
            document, _ = backend.read_reverse_analysis(project_id, analysis_id)
            current_revision = int(document.get('revision', 0))
            if request.expected_revision != current_revision:
                raise HTTPException(409, {'code': 'analysis_revision_conflict', 'message': '参考拆解已更新，请重新读取并核对后再转为制作草案', 'current_revision': current_revision, 'retryable': False})
        return backend._convert_reverse_analysis(project_id, analysis_id)


def _convert_reverse_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
    document, analysis_path = backend.read_reverse_analysis(project_id, analysis_id)
    if not document.get("timeline"):
        raise HTTPException(422, "参考拆解尚无时间段，请先完成拆解")
    manifest = backend.load_project_manifest(project_id)
    workspace = backend.resolve_registered_path(str(manifest.get("workspace_root")))
    if not manifest.get("plan_path"):
        plan_path = workspace / "plans" / "plan.json"
        manifest["plan_path"] = backend.root_relative_path(plan_path)
        backend.save_project_manifest(manifest)
        if project_id == backend.CURRENT_PROJECT_ID:
            backend.apply_project_manifest(manifest)
    plan, plan_path = backend.load_project_plan(project_id)
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
        backend.persist_plan_edit(project_id, plan, plan_path)
    for segment in created:
        backend.record_segment_artifacts(project_id, segment, "reverse-analysis")
    seed = {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "project_id": project_id,
        "created_segments": [item["id"] for item in segments if isinstance(item, dict) and item.get("source_analysis") == analysis_id],
        "artifacts": document.get("artifacts", {}),
        "note": "参考拆解仅生成制作草案；角色、场景、动作与传播机制仍需人工确认。",
    }
    saved_seed = document.get("production_seed")
    seed_path = backend.resolve_registered_path(saved_seed) if saved_seed else analysis_path.parent / "production-seed.json"
    if created and seed_path.exists():
        seed_path = analysis_path.parent / f"production-seed-{time.time_ns()}.json"
    if created or not seed_path.exists():
        seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    document["status"] = "converted_to_production"
    for stage in document.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == "production":
            stage["status"] = "done"
    document["production_seed"] = backend.root_relative_path(seed_path)
    backend.write_reverse_analysis(document, analysis_path)
    return {"analysis": document, "created_segments": created, "plan": plan, "production_seed": backend.root_relative_path(seed_path)}
