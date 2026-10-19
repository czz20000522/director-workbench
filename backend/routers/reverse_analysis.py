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
from ..schemas import (
    ReferenceAudioExtractRequest,
    ReferenceAsrRequest,
    ReferenceRangeExtractRequest,
    ReferenceSampleRequest,
    ReverseArtifactUpdateRequest,
    ReverseClassificationRequest,
    ReverseConvertRequest,
    ReverseTimelineUpdateRequest,
)
from .. import app as backend
from tools import reference_decompose


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
            "role": "observation_candidates",
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
    return document


@ROUTER.post("/api/projects/{project_id}/reverse-analyses", status_code=202)
async def create_reverse_analysis(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    backend.require_project_manifest(project_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".flac", ".m4a", ".aac"}:
        raise HTTPException(422, "只支持常见音视频参考素材")
    analysis_id = f"ref-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    output = backend.reverse_analysis_path(project_id, analysis_id)
    source_dir = output / "source"
    source_dir.mkdir(parents=True, exist_ok=False)
    source = source_dir / f"reference{suffix}"
    await backend.stream_upload(file, source)
    try:
        facts = reference_decompose.media_facts(source)
    except Exception as exc:
        document = {
            'schema_version': 2, 'id': analysis_id, 'project_id': project_id,
            'title': file.filename or '参考素材', 'status': 'registration_failed',
            'source': {'original_name': file.filename or source.name, 'path': backend.root_relative_path(source),
                       'probe_error': str(exc)},
            'stages': [{'id': 'import', 'status': 'done'}, {'id': 'probe', 'status': 'failed'}],
            'observations': [], 'timeline': [], 'artifacts': {},
        }
        backend.write_reverse_analysis(document, output / 'analysis.json')
        raise HTTPException(422, f'参考素材媒体探测失败: {exc}') from exc
    document = {
        'schema_version': 2, 'id': analysis_id, 'project_id': project_id,
        'title': file.filename or '参考素材', 'status': 'registered', 'created_at': time.time(),
        'source': {'asset_id': f'reference-{analysis_id}', 'original_name': file.filename or source.name,
                   'path': backend.root_relative_path(source), 'size_bytes': source.stat().st_size,
                   'registered': True, **facts},
        'stages': [
            {'id': 'import', 'label': '参考素材登记', 'status': 'done', 'executor': 'script'},
            {'id': 'probe', 'label': '媒体探测', 'status': 'done', 'executor': 'script'},
            {'id': 'observation', 'label': '显式观察工具', 'status': 'ready', 'executor': 'caller'},
            {'id': 'semantic', 'label': '模型观察', 'status': 'waiting', 'executor': 'caller'},
            {'id': 'review', 'label': '人工校订', 'status': 'waiting', 'executor': 'human'},
            {'id': 'production', 'label': '转入正向生产', 'status': 'waiting', 'executor': 'caller'},
        ],
        'observations': [], 'timeline': [],
        'artifacts': {'source': {
            'title': '源素材档案', 'status': 'ready', 'provenance': 'observable_fact',
            'summary': f"{facts.get('width') or '?'}x{facts.get('height') or '?'} · {facts.get('duration_seconds', 0):.2f}s · {facts.get('fps') or '?'}fps",
            'facts': facts, 'evidence': [backend.root_relative_path(source)], 'confidence': 1.0,
            'user_status': 'unreviewed',
        }},
    }
    with backend.REVERSE_DOCUMENT_LOCK:
        backend.write_reverse_analysis(document, output / 'analysis.json')
    return backend.get_reverse_analysis(project_id, analysis_id)


def _reference_source(document: dict[str, Any]) -> Path:
    source = document.get('source') if isinstance(document.get('source'), dict) else {}
    value = source.get('path')
    if not value:
        raise HTTPException(422, '参考素材缺少已登记路径')
    try:
        path = backend.resolve_registered_path(str(value))
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(422, f'参考素材路径无法解析: {exc}') from exc
    if not path.is_file():
        raise HTTPException(404, '参考素材文件不存在')
    return path


def _append_observation(document: dict[str, Any], observation: dict[str, Any]) -> None:
    observations = document.setdefault('observations', [])
    if not isinstance(observations, list):
        observations = []
        document['observations'] = observations
    observations.append(observation)
    timeline = document.setdefault('timeline', [])
    if not isinstance(timeline, list):
        timeline = []
        document['timeline'] = timeline
    # Timeline is an observation index only. It is never treated as a shot list.
    timeline.append(observation)


def _has_explicit_frame_observation(document: dict[str, Any]) -> bool:
    """Return whether the caller has supplied at least one usable frame."""
    timeline = document.get('timeline') if isinstance(document.get('timeline'), list) else []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if item.get('frame') or item.get('frame_path'):
            return True
        if any(isinstance(frame, dict) and frame.get('path') for frame in item.get('frames') or []):
            return True
    return False


@ROUTER.post('/api/projects/{project_id}/reverse-analyses/{analysis_id}/observations/range', status_code=201)
def extract_reference_range(project_id: str, analysis_id: str, request: ReferenceRangeExtractRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        source = _reference_source(document)
        facts = document.get('source') if isinstance(document.get('source'), dict) else {}
        observation_id = f"range-{uuid.uuid4().hex[:10]}"
        try:
            extracted = reference_decompose.extract_explicit_range(
                source, path.parent / 'observations', facts, request.start_seconds, request.end_seconds,
                frame_seconds=request.frame_seconds, artifact_id=observation_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, f'显式参考范围提取失败: {exc}') from exc
        observation = {
            'id': observation_id, 'type': 'explicit_range', 'start_seconds': extracted['start_seconds'],
            'end_seconds': extracted['end_seconds'], 'frame_seconds': request.frame_seconds,
            'segment_path': backend.root_relative_path(extracted['segment_path']),
            'frame_path': backend.root_relative_path(extracted['frame_path']) if extracted['frame_path'] else None,
            'provenance': 'observable_fact', 'status': 'pending_review', 'user_status': 'unreviewed',
            'is_shot_cut': False, 'conversion_source': 'caller_must_submit_shot_list',
        }
        _append_observation(document, observation)
        backend.write_reverse_analysis(document, path)
        return {'observation': observation, 'analysis': document}


@ROUTER.post('/api/projects/{project_id}/reverse-analyses/{analysis_id}/observations/sample', status_code=201)
def sample_reference_frames(project_id: str, analysis_id: str, request: ReferenceSampleRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        source = _reference_source(document)
        facts = document.get('source') if isinstance(document.get('source'), dict) else {}
        observation_id = f"sample-{uuid.uuid4().hex[:10]}"
        try:
            frames = reference_decompose.extract_explicit_frames(
                source, path.parent / 'observations', facts, request.times_seconds, sample_id=observation_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, f'显式采样失败: {exc}') from exc
        observation = {
            'id': observation_id, 'type': 'explicit_sampling', 'sampling_mode': 'caller_selected',
            'times_seconds': [round(value, 3) for value in request.times_seconds],
            'frames': [{'time_seconds': item['time_seconds'], 'path': backend.root_relative_path(item['frame_path'])} for item in frames],
            'provenance': 'observable_fact', 'status': 'pending_review', 'user_status': 'unreviewed',
            'is_shot_cut': False, 'review_label': '等距采样/待确认范围',
            'conversion_source': 'caller_must_submit_shot_list',
        }
        _append_observation(document, observation)
        backend.write_reverse_analysis(document, path)
        return {'observation': observation, 'analysis': document}


@ROUTER.post('/api/projects/{project_id}/reverse-analyses/{analysis_id}/observations/audio', status_code=201)
def extract_reference_audio(project_id: str, analysis_id: str, request: ReferenceAudioExtractRequest) -> dict[str, Any]:
    """提取调用方明确指定范围的音频观察，不运行 ASR 或模型。"""
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        source = _reference_source(document)
        facts = document.get('source') if isinstance(document.get('source'), dict) else {}
        if not facts.get('has_audio'):
            raise HTTPException(422, '参考素材没有可提取的音轨')
        observation_id = f"audio-{uuid.uuid4().hex[:10]}"
        try:
            audio_path = reference_decompose.extract_explicit_audio(
                source, path.parent / 'observations', facts, request.start_seconds, request.end_seconds,
                artifact_id=observation_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, f'显式音频范围提取失败: {exc}') from exc
        observation = {
            'id': observation_id, 'type': 'explicit_audio',
            'start_seconds': round(request.start_seconds, 3), 'end_seconds': round(request.end_seconds, 3),
            'audio_path': backend.root_relative_path(audio_path), 'provenance': 'observable_fact',
            'status': 'pending_review', 'user_status': 'unreviewed',
            'review_label': '明确范围音频/待确认', 'conversion_source': 'caller_must_submit_shot_list',
        }
        _append_observation(document, observation)
        backend.write_reverse_analysis(document, path)
        return {'observation': observation, 'analysis': document}


@ROUTER.post('/api/projects/{project_id}/reverse-analyses/{analysis_id}/observations/asr', status_code=201)
def transcribe_reference(project_id: str, analysis_id: str, _request: ReferenceAsrRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, path = backend.read_reverse_analysis(project_id, analysis_id)
        source = _reference_source(document)
        facts = document.get('source') if isinstance(document.get('source'), dict) else {}
        if not facts.get('has_audio'):
            raise HTTPException(422, '参考素材没有可供 ASR 使用的音轨')
        working = path.parent / 'observations' / 'asr'
        try:
            audio_path = reference_decompose.extract_audio_for_asr(source, working)
            transcript, error = reference_decompose.transcribe_audio(audio_path, working)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, f'显式 ASR 失败: {exc}') from exc
        if transcript is None:
            raise HTTPException(409, error or 'ASR 运行环境未就绪')
        observation_id = f"asr-{uuid.uuid4().hex[:10]}"
        observation = {
            'id': observation_id, 'type': 'asr', 'audio_path': backend.root_relative_path(audio_path),
            'transcript_path': backend.root_relative_path(working / 'transcript.raw.json'),
            'transcript': transcript, 'provenance': 'model_inference', 'status': 'candidate_needs_review',
            'user_status': 'unreviewed', 'review_label': '模型候选/待校订',
        }
        _append_observation(document, observation)
        transcript_artifact = document.setdefault('artifacts', {}).setdefault('transcript', {
            'title': '台词与歌词候选', 'evidence': [],
        })
        transcript_artifact.update({
            'status': 'candidate_needs_review', 'provenance': 'model_inference',
            'summary': f"显式 ASR 生成 {len(transcript.get('segments', []))} 个候选片段；必须人工核对",
            'evidence': [observation['transcript_path']], 'confidence': 0.25,
            'user_status': 'unreviewed', 'model_candidate': observation,
        })
        backend.write_reverse_analysis(document, path)
        return {'observation': observation, 'analysis': document}


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
    if not _has_explicit_frame_observation(document):
        raise HTTPException(422, {
            'code': 'explicit_observation_required',
            'message': '请先通过显式范围或采样工具提取至少一帧，再运行模型观察',
            'action': 'extract_reference_range_or_sample',
        })
    root = backend.analysis_job_root()
    with backend.GPU_ADMISSION_LOCK, background_jobs.LOCK:
        previous = next((job for job in background_jobs.records(root) if job["project_id"] == project_id and job["analysis_id"] == analysis_id and job["status"] in background_jobs.ACTIVE), None)
        if previous and previous.get('task_id'):
            task = backend.get_task(previous['task_id'])
            if task and task['status'] in {'succeeded', 'failed', 'stopped'}:
                previous = None
        if previous:
            return backend.current_semantic_job(project_id, analysis_id)
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
                       'scheduler_dependencies': []}
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
def convert_reverse_analysis(project_id: str, analysis_id: str, request: ReverseConvertRequest) -> dict[str, Any]:
    with backend.REVERSE_DOCUMENT_LOCK:
        document, _ = backend.read_reverse_analysis(project_id, analysis_id)
        current_revision = int(document.get('revision', 0))
        if request.expected_revision != current_revision:
            raise HTTPException(409, {'code': 'analysis_revision_conflict', 'message': '参考拆解已更新，请重新读取并核对后再转为制作草案', 'current_revision': current_revision, 'expected_revision': request.expected_revision, 'retryable': False})
        return backend._convert_reverse_analysis(project_id, analysis_id, request.shots)


def _convert_reverse_analysis(project_id: str, analysis_id: str, shots: list[Any]) -> dict[str, Any]:
    document, analysis_path = backend.read_reverse_analysis(project_id, analysis_id)
    if not shots:
        raise HTTPException(422, "必须提交至少一个明确制作分镜")
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
            linked.add(str(item.get("source_shot_id")))
        for entry in item.get("version_history") or []:
            if isinstance(entry, dict) and isinstance(entry.get("merged_segment"), dict):
                pending.append(entry["merged_segment"])
    created: list[dict[str, Any]] = []
    allowed_fields = {'location', 'shot_size', 'camera', 'wardrobe', 'performance', 'prompt', 'workflow',
                      'first_frame', 'last_frame', 'audio_guide', 'delivery_master', 'script_scene_id'}
    source_facts = document.get('source') if isinstance(document.get('source'), dict) else {}
    for index, draft in enumerate(shots, start=1):
        values = draft.model_dump() if hasattr(draft, 'model_dump') else dict(draft)
        start_seconds = float(values['start_seconds'])
        end_seconds = float(values['end_seconds'])
        try:
            reference_decompose.validate_explicit_range(source_facts, start_seconds, end_seconds)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        fields = values.get('fields') if isinstance(values.get('fields'), dict) else {}
        source_shot_id = str(values.get('segment_id') or f"{start_seconds:.3f}-{end_seconds:.3f}-{index}")
        if source_shot_id in linked:
            continue
        base_id = f"REF-{analysis_id[-8:]}-{index:02d}"
        segment_id = base_id
        counter = 2
        while segment_id in known:
            segment_id = f"{base_id}-{counter}"
            counter += 1
        known.add(segment_id)
        segment = {
            "id": segment_id,
            "duration_seconds": round(end_seconds - start_seconds, 3),
            "status": "planned",
            **{key: fields.get(key) for key in allowed_fields},
            "keyframes": {"first": None, "last": None},
            "source_analysis": analysis_id,
            "source_shot_id": source_shot_id,
            "source_revision": int(document.get("revision", 0)),
            "source_range_seconds": [start_seconds, end_seconds],
            "source_note_status": "pending_review",
            "source_observation": str(fields.get("source_observation") or ""),
            "source_user_note": str(fields.get("source_user_note") or ""),
        }
        created.append(segment)
        segments.append(segment)
        linked.add(source_shot_id)
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
        "note": "制作分镜来自调用方提交的明确时间段和字段；模型观察仅作为待校订证据。",
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
