"""One dialogue shot = speech stage + H3 audio-conditioned video stage.

The public contract is one task per shot.  The speech stage runs first and its
WAV becomes both the H3 drive audio and the authoritative dialogue track.  No
separate public speech task exists; the task payload records both stages so a
failed video can be retried without losing the voice.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import dialogue_shot_recipe as recipe

SCHEMA_VERSION = 1
MAX_REFERENCES = 8
TTS_TIMEOUT_SECONDS = float(os.environ.get('DIRECTOR_INDEX_TTS_TIMEOUT_SECONDS', '600'))
PREVIEW_ASPECT_SIZES = {
    '16:9': (864, 480),
    '9:16': (480, 864),
    '1:1': (640, 640),
}


class DialogueShotError(RuntimeError):
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
            'retryable': self.status_code in {409, 502, 503, 504},
            **self.details,
        }
        if self.blockers:
            payload['blockers'] = self.blockers
        return payload


class DialogueShotRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    segment_id: str = Field(min_length=1, max_length=160)
    voice_reference_asset_id: str = Field(min_length=1, max_length=160)
    dialogue: str = Field(min_length=1, max_length=2000)
    language: str = Field(default='Chinese', max_length=40)
    emotion: list[float] = Field(default_factory=list, max_length=8)
    duration_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    first_frame_asset_id: str = Field(min_length=1, max_length=160)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=MAX_REFERENCES)
    prompt: str = Field(default='', max_length=8000)
    duration_seconds: float = Field(default=5.0, ge=4.0, le=15.0)
    aspect_ratio: str = Field(default='16:9', max_length=8)
    seed: int = Field(default=42, ge=0, le=2147483647)
    steps: int = Field(default=8, ge=1, le=100)


def _backend():
    from . import app as backend
    return backend


def _asset(backend: Any, manifest: dict[str, Any], asset_id: str, *, kind: str, role: str) -> tuple[dict[str, Any], Path]:
    asset = next((item for item in manifest.get('assets', [])
                  if isinstance(item, dict) and str(item.get('id')) == asset_id), None)
    if asset is None:
        raise DialogueShotError(
            'asset_not_owned', f'{role}素材 {asset_id} 不属于当前作品。', 403,
            blockers=[{'code': 'asset_not_owned', 'asset_id': asset_id, 'role': role}],
        )
    if asset.get('kind') != kind:
        raise DialogueShotError(
            'asset_kind_mismatch', f'{role}素材 {asset_id} 不是 {kind} 类型。', 422,
            blockers=[{'code': 'asset_kind_mismatch', 'asset_id': asset_id,
                       'role': role, 'kind': asset.get('kind')}],
        )
    value = asset.get('image_path') if kind == 'image' else (asset.get('sources') or {}).get('A')
    if not value:
        raise DialogueShotError(
            'asset_unavailable', f'{role}素材 {asset_id} 缺少已登记文件。', 409,
            blockers=[{'code': 'asset_unavailable', 'asset_id': asset_id, 'role': role}],
        )
    try:
        path = backend.resolve_registered_path(str(value))
    except (OSError, ValueError) as exc:
        raise DialogueShotError('asset_unavailable', f'{role}素材不可读取: {exc}', 409) from exc
    if not path.is_file():
        raise DialogueShotError('asset_unavailable', f'{role}素材文件不存在。', 409)
    return asset, path


def _copy(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    shutil.copy2(source, destination)
    after = source.stat()
    copied = destination.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or copied.st_size != before.st_size:
        destination.unlink(missing_ok=True)
        raise DialogueShotError('asset_changed', '素材在复制期间被修改。', 409)
    return {'source': str(source), 'path': str(destination),
            'size_bytes': copied.st_size, 'mtime_ns': copied.st_mtime_ns}


def _register_dialogue_asset(backend: Any, project_id: str, output: Path,
                             segment_id: str, task_id: str) -> dict[str, Any]:
    manifest = backend.require_project_manifest(project_id)
    workspace_value = manifest.get('workspace_root')
    workspace = backend.resolve_registered_path(str(workspace_value)) if workspace_value else output.parent
    try:
        output.relative_to(workspace)
    except ValueError as exc:
        raise DialogueShotError('dialogue_path_escape', '对白文件不在作品目录内。', 500) from exc
    asset_id = f'audio-dialogue-{task_id[:12]}'
    relative = backend.root_relative_path(output)
    assets = manifest.setdefault('assets', [])
    if not any(item.get('id') == asset_id for item in assets if isinstance(item, dict)):
        assets.append({
            'id': asset_id,
            'kind': 'audio',
            'title': f'对白 · {segment_id}',
            'subtitle': 'IndexTTS 生成对白',
            'ready': True,
            'origin': 'IndexTTS-2.5',
            'sources': {'A': relative},
            'audio_path': relative,
            'dialogue_shot_task_id': task_id,
        })
    manifest.setdefault('media', {})[Path(relative).name] = relative
    backend.save_project_manifest(manifest)
    return {'asset_id': asset_id, 'path': relative, 'size_bytes': output.stat().st_size}


def _tts_command(reference: Path, text: str, output: Path, request: DialogueShotRequest) -> list[str]:
    home_value = os.environ.get('DIRECTOR_INDEX_TTS_HOME', '').strip()
    if not home_value:
        raise DialogueShotError('index_tts_unavailable',
                                '未配置 DIRECTOR_INDEX_TTS_HOME。', 503,
                                blockers=[{'code': 'index_tts_unavailable',
                                           'message': '请在工作台服务环境配置 IndexTTS 目录。'}])
    home = Path(home_value)
    python = Path(os.environ.get('DIRECTOR_INDEX_TTS_PYTHON',
                                 str(home / '.venv' / 'Scripts' / 'python.exe')))
    script = Path(__file__).resolve().parents[1] / 'tools' / 'index_tts_generate.py'
    if not python.is_file() or not script.is_file():
        raise DialogueShotError('index_tts_unavailable',
                                'IndexTTS 运行环境或生成脚本不存在。', 503,
                                blockers=[{'code': 'index_tts_unavailable',
                                           'python': str(python), 'script': str(script)}])
    return [
        str(python), str(script), '--reference', str(reference),
        '--text', text, '--output', str(output),
        '--language', request.language,
        '--duration-factor', str(request.duration_factor),
        '--emotion', json.dumps(request.emotion),
        '--index-tts-home', str(home),
    ]


def create_task(project_id: str, request: DialogueShotRequest) -> dict[str, Any]:
    backend = _backend()
    manifest = backend.require_project_manifest(project_id)
    document, plan_path = backend.load_project_plan(project_id)
    segment = next((item for item in document.get('segments', [])
                    if isinstance(item, dict) and str(item.get('id')) == request.segment_id), None)
    if segment is None:
        raise DialogueShotError('segment_not_found', f'目标分镜不存在: {request.segment_id}', 404)
    if request.aspect_ratio not in PREVIEW_ASPECT_SIZES:
        raise DialogueShotError('invalid_aspect_ratio', '画幅必须是 16:9、9:16 或 1:1。', 422)
    voice_asset, voice_path = _asset(backend, manifest, request.voice_reference_asset_id,
                                     kind='audio', role='音色参考')
    first_asset, first_path = _asset(backend, manifest, request.first_frame_asset_id,
                                     kind='image', role='首帧图')
    reference_pairs = [_asset(backend, manifest, asset_id, kind='image', role='参考图')
                       for asset_id in request.reference_asset_ids]
    backend.ensure_asset_not_pending(project_id, request.segment_id)

    task_id = str(uuid.uuid4())
    workspace_value = manifest.get('workspace_root')
    workspace = backend.resolve_registered_path(str(workspace_value)) if workspace_value else backend.DIRECTOR_WORKSPACES_ROOT / project_id
    dialogue_dir = workspace / 'assets' / 'dialogue' / task_id
    dialogue_file = dialogue_dir / 'voice.wav'
    dialogue_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    workflow_path: Path | None = None
    try:
        command = _tts_command(voice_path, request.dialogue, dialogue_file, request)
        started = time.time()
        try:
            completed = subprocess.run(command, cwd=str(Path(__file__).resolve().parents[1]),
                                       capture_output=True, text=True,
                                       timeout=TTS_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DialogueShotError('speech_timeout', 'IndexTTS 生成对白超时。', 504) from exc
        if completed.returncode != 0 or not dialogue_file.is_file():
            tail = (completed.stderr or completed.stdout or '')[-2000:]
            raise DialogueShotError('speech_failed', 'IndexTTS 生成对白失败。', 502,
                                    blockers=[{'code': 'speech_failed', 'detail': tail}])
        voice = _register_dialogue_asset(backend, project_id, dialogue_file,
                                         request.segment_id, task_id)
        width, height = PREVIEW_ASPECT_SIZES[request.aspect_ratio]
        input_root = Path(backend.INPUT_ROOT)
        prefix = f'dsh-dialogue-{task_id}'
        first_name = f'{prefix}-first{first_path.suffix.lower()}'
        audio_name = f'{prefix}.wav'
        staged.append(input_root / first_name)
        staged.append(input_root / audio_name)
        _copy(first_path, input_root / first_name)
        _copy(dialogue_file, input_root / audio_name)
        reference_names = []
        for index, (_, source) in enumerate(reference_pairs, start=1):
            name = f'{prefix}-ref-{index:02d}{source.suffix.lower()}'
            staged.append(input_root / name)
            _copy(source, input_root / name)
            reference_names.append(name)
        prompt = request.prompt.strip() or (
            '中近景，人物正脸或四分之三侧，嘴部无遮挡，开口说话，'
            '嘴部动作跟随 <Audio 1>；固定机位，低饱和胶片，写实，'
            '画面无字幕、无可读文字、无水印。'
        )
        graph = recipe.build_graph(
            first_frame_name=first_name,
            audio_name=audio_name,
            prompt=prompt,
            duration_seconds=request.duration_seconds,
            seed=request.seed,
            output_prefix=f'导演工作台工作目录/{project_id}/{request.segment_id}/dialogue-{task_id}',
            reference_names=reference_names,
            width=width,
            height=height,
            steps=request.steps,
        )
        snapshot_root = backend.task_snapshot_root(project_id)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        workflow_path = snapshot_root / f'dialogue-{task_id}.json'
        backend.write_json_atomic(workflow_path, graph)
        execution_snapshot = {
            'api_graph': str(workflow_path),
            'prompt': prompt,
            'parameters': {
                'duration_seconds': request.duration_seconds,
                'aspect_ratio': request.aspect_ratio,
                'width': width,
                'height': height,
                'seed': request.seed,
                'steps': request.steps,
            },
            'materials': {
                'first_frame': str(first_path),
                'voice_reference': str(voice_path),
                'dialogue': str(dialogue_file),
                'references': [str(path) for _, path in reference_pairs],
            },
            'dialogue_shot': {
                'schema_version': SCHEMA_VERSION,
                'recipe': recipe.RECIPE_ID,
                'stages': {
                    'speech': {
                        'status': 'succeeded',
                        'engine': 'IndexTTS-2.5',
                        'voice_reference_asset_id': request.voice_reference_asset_id,
                        'voice_reference_path': str(voice_path),
                        'dialogue': request.dialogue,
                        'language': request.language,
                        'emotion': list(request.emotion),
                        'duration_factor': request.duration_factor,
                        'duration_seconds': round(time.time() - started, 3),
                        'asset_id': voice['asset_id'],
                        'path': voice['path'],
                        'size_bytes': voice['size_bytes'],
                    },
                    'video': {'status': 'queued', 'recipe': recipe.RECIPE_ID},
                },
                'first_frame_asset_id': request.first_frame_asset_id,
                'reference_asset_ids': list(request.reference_asset_ids),
                'voice_asset_id': voice['asset_id'],
                'dialogue_asset_path': voice['path'],
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
            'prompt': prompt,
            'dialogue_shot': execution_snapshot['dialogue_shot'],
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
        backend.queue_task(task_id)
        result = backend.get_task(task_id) or {'id': task_id, 'status': 'scheduler_waiting'}
        result['speech'] = execution_snapshot['dialogue_shot']['stages']['speech']
        return result
    except BaseException:
        if workflow_path is not None and not workflow_path.exists():
            pass
        for path in staged:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def get_task(project_id: str, task_id: str) -> dict[str, Any]:
    backend = _backend()
    task = backend.project_task_detail(project_id, task_id)
    payload = task.get('payload') if isinstance(task.get('payload'), dict) else {}
    if payload.get('kind') != 'workflow' or not isinstance(payload.get('dialogue_shot'), dict):
        raise DialogueShotError('dialogue_shot_not_found',
                                f'该作品中不存在对白镜头任务: {task_id}', 404)
    return task
