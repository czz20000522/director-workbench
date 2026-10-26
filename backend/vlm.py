"""Read-only vision-language observations through a local Ollama service.

The user names the frame and the question; the model answers about that frame only.
Nothing here writes to the plan, adopts a candidate or registers a creative asset —
an observation is stored as this task's receipt and is labelled 待人工核对.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from . import creative_inspection, media_operations
from .workbench_config import MATERIAL_EXTENSIONS, RUNTIME

SERVICE_URL = os.environ.get('DIRECTOR_VLM_URL', 'http://127.0.0.1:11434').rstrip('/')
MODEL_ID = os.environ.get('DIRECTOR_VLM_MODEL', 'qwen2.5vl:7b')
OLLAMA_EXECUTABLE = os.environ.get('DIRECTOR_OLLAMA_EXECUTABLE', '')
PROVIDERS = ('local-ollama', 'deepseek-vision')
CLOUD_PROVIDER = 'deepseek-vision'
CLOUD_DEFAULT_BASE_URL = 'https://api.deepseek.com'
CLOUD_DEFAULT_MODEL = 'deepseek-flash'
MAX_IMAGE_BYTES = 32 * 1024**2
MAX_VIDEO_BYTES = 2 * 1024**3
MAX_PROMPT = 2000
RUN_TIMEOUT = 900
START_TIMEOUT = 45

_start_lock = threading.Lock()
_service_process: subprocess.Popen | None = None


class VlmStopped(RuntimeError):
    pass


class VlmFailed(RuntimeError):
    pass


def cloud_config() -> dict[str, Any]:
    """Read the optional cloud endpoint config; it lives in the data root, never in the repo."""
    from . import app as backend
    document: dict[str, Any] = {}
    path = Path(backend.ROOT) / 'config' / 'vision-provider.json'
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            document = loaded
    except FileNotFoundError:
        document = {}
    except (OSError, ValueError):
        raise ValueError('云端观察配置无法读取，请检查 config/vision-provider.json')
    section = document.get('deepseek') if isinstance(document.get('deepseek'), dict) else {}
    api_key = str(os.environ.get('DEEPSEEK_API_KEY') or section.get('api_key') or '').strip()
    return {'api_key': api_key,
            'base_url': str(section.get('base_url') or CLOUD_DEFAULT_BASE_URL).rstrip('/'),
            'model': str(section.get('model') or CLOUD_DEFAULT_MODEL).strip()}


def _request_json(path: str, *, timeout: float = 2.0) -> dict[str, Any]:
    # trust_env=False: Ollama is a loopback service and must never be routed through
    # a system proxy, which would turn a healthy 127.0.0.1 request into a 502.
    response = httpx.get(f'{SERVICE_URL}{path}', timeout=timeout, trust_env=False)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('Ollama 返回了非对象响应')
    return payload


def _service_models(*, timeout: float = 3.0) -> list[str]:
    payload = _request_json('/api/tags', timeout=timeout)
    models = payload.get('models')
    if not isinstance(models, list):
        return []
    return [str(item.get('name')) for item in models if isinstance(item, dict) and item.get('name')]


def _ollama_executable() -> str:
    return OLLAMA_EXECUTABLE or shutil.which('ollama') or ''


def _model_manifest(model: str) -> Path | None:
    """Ask the model store directly: a stopped service must not look like a missing model."""
    root = Path(os.environ.get('OLLAMA_MODELS') or (Path.home() / '.ollama' / 'models'))
    name, _, tag = str(model).partition(':')
    if not name:
        return None
    try:
        for candidate in (root / 'manifests').glob(f'*/library/{name.lower()}/{tag or "latest"}'):
            if candidate.exists():
                return candidate
    except OSError:
        return None
    return None


def _local_state() -> dict[str, Any]:
    """Report the Ollama provider, distinguishing "not running" from "not installed"."""
    try:
        names = _service_models()
        running = True
    except (OSError, ValueError, httpx.HTTPError):
        names, running = [], False
    if MODEL_ID in names:
        return {'available': True, 'running': True, 'missing': [], 'model': MODEL_ID, 'service': SERVICE_URL}
    manifest = _model_manifest(MODEL_ID)
    executable = _ollama_executable()
    if manifest and executable:
        return {'available': True, 'running': False, 'missing': [], 'model': MODEL_ID, 'service': SERVICE_URL,
                'reason': 'Ollama 当前没有运行；点生成时工作台会自动拉起，首次约 10–20 秒。'}
    missing = ([f'{MODEL_ID} 模型'] if not manifest else []) + ([] if executable else ['Ollama 可执行文件'])
    return {'available': False, 'running': running, 'missing': missing or ['Ollama 服务'],
            'model': MODEL_ID, 'service': SERVICE_URL,
            'reason': '' if running else 'Ollama 未响应，且本机模型或可执行文件不完整。'}


def capability() -> dict[str, Any]:
    """Report every observation provider; probing never starts Ollama and never sends a frame."""
    local = _local_state()
    try:
        cloud = cloud_config()
        cloud_error = ''
    except ValueError as exc:
        cloud, cloud_error = {'api_key': '', 'base_url': CLOUD_DEFAULT_BASE_URL, 'model': CLOUD_DEFAULT_MODEL}, str(exc)
    local.update({'label': '本机 Ollama', 'note': '画面不出机器；成人向内容只能用这一路。'})
    providers = {
        'local-ollama': local,
        CLOUD_PROVIDER: {'available': bool(cloud['api_key']) and not cloud_error, 'model': cloud['model'],
                         'service': cloud['base_url'], 'api_key_present': bool(cloud['api_key']),
                         'missing': [] if cloud['api_key'] and not cloud_error else ['DeepSeek API Key'],
                         'label': 'DeepSeek 视觉',
                         'note': '画面会发送到外部 API。平台对色情内容有自动风控与封号策略，'
                                 '成人向或未成年人相关的帧都不要用这一路。'},
    }
    if cloud_error:
        providers[CLOUD_PROVIDER]['reason'] = cloud_error
    elif not cloud['api_key']:
        providers[CLOUD_PROVIDER]['reason'] = '未配置 API Key（config/vision-provider.json 或 DEEPSEEK_API_KEY）。'
    return {'available': any(item['available'] for item in providers.values()), 'missing': local['missing'],
            'engine': 'Qwen2.5-VL / DeepSeek 视觉', 'id': 'vlm-observation', 'model': MODEL_ID, 'service': SERVICE_URL,
            'providers': providers, 'default_provider': 'local-ollama', 'reason': local.get('reason', ''),
            'note': '只读观察：只回答你指定的这一帧，不写入分镜、不自动采用'}


def _ensure_service_online() -> None:
    try:
        _request_json('/api/version', timeout=2.0)
        return
    except (OSError, ValueError, httpx.HTTPError):
        pass
    with _start_lock:
        try:
            _request_json('/api/version', timeout=2.0)
            return
        except (OSError, ValueError, httpx.HTTPError):
            pass
        executable = _ollama_executable()
        if not executable or not Path(executable).is_file():
            raise VlmFailed('本机未找到 Ollama 可执行文件，请手动启动 Ollama 后重试')
        global _service_process
        if _service_process is None or _service_process.poll() is not None:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            log_path = RUNTIME / 'ollama-serve.log'
            with log_path.open('ab') as log:
                _service_process = subprocess.Popen([executable, 'serve'], stdout=log, stderr=subprocess.STDOUT,
                                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            try:
                _request_json('/api/version', timeout=2.0)
                return
            except (OSError, ValueError, httpx.HTTPError):
                time.sleep(0.5)
        raise VlmFailed(f'Ollama 在 {START_TIMEOUT:.0f} 秒内没有就绪，请查看运行目录里的 ollama-serve.log')


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
        raise ValueError('观察任务 ID 无效')
    if task_id.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('观察任务 ID 是保留名称')


def _extract_frame(source: Path, frame_seconds: float, destination: Path) -> None:
    command = [str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y',
               '-ss', f'{frame_seconds:.3f}', '-i', str(source), '-frames:v', '1', '-q:v', '2', str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'无法从该时间点取出画面：{exc}') from exc
    if not destination.is_file() or destination.stat().st_size < 1024:
        raise ValueError('取出的画面无效，请换一个时间点')


def prepare(task_id: str, source: Path, snapshot_root: Path, *, prompt: str, frame_seconds: float | None,
            temperature: float, num_predict: int, provider: str = 'local-ollama') -> dict:
    """Freeze the frame, the question and the model source before the task is queued."""
    _validate_task_id(task_id)
    if provider not in PROVIDERS:
        raise ValueError('未知观察来源')
    if provider == CLOUD_PROVIDER:
        cloud = cloud_config()
        if not cloud['api_key']:
            raise ValueError('云端观察还没有配置 API Key，请改用本机 Ollama')
        if not cloud['model']:
            raise ValueError('云端观察没有配置模型名称')
        model_id, service = cloud['model'], cloud['base_url']
    else:
        model_id, service = MODEL_ID, SERVICE_URL
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError('观察源必须是已存在的文件')
    suffix = source.suffix.lower()
    if suffix in MATERIAL_EXTENSIONS['image']:
        kind = 'image'
    elif suffix in MATERIAL_EXTENSIONS['video']:
        kind = 'video'
    else:
        raise ValueError('画面观察只接受图片或视频素材')
    size = source.stat().st_size
    if kind == 'image' and size > MAX_IMAGE_BYTES:
        raise ValueError('观察图片不超过 32 MiB')
    if kind == 'video' and size > MAX_VIDEO_BYTES:
        raise ValueError('观察视频不超过 2 GiB')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError(f'观察问题须为 1 至 {MAX_PROMPT} 字符')
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError('采样温度须在 0 至 2 之间')
    if type(num_predict) is not int or not 16 <= num_predict <= 2048:
        raise ValueError('回答长度须为 16 至 2048')
    facts = creative_inspection.probe(source)
    if kind == 'video':
        duration = facts.get('duration_seconds')
        if not facts.get('known') or facts.get('width') is None \
                or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('无法读取该视频的时长或画面轨')
        if frame_seconds is None:
            raise ValueError('视频观察必须明确指定取帧时间点')
        if type(frame_seconds) not in (int, float) or not math.isfinite(frame_seconds) or frame_seconds < 0:
            raise ValueError('取帧时间点必须是非负有限数值')
        if frame_seconds >= duration:
            raise ValueError(f'取帧时间点超出视频时长（{duration:.2f} 秒）')
    elif frame_seconds is not None:
        raise ValueError('图片观察不接受取帧时间点')

    root = Path(snapshot_root)
    if not root.is_absolute():
        raise ValueError('观察任务根目录必须是绝对路径')
    directory = root.resolve() / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen_source = directory / (f'source{suffix}')
    shutil.copyfile(source, frozen_source)
    if kind == 'video':
        frame = directory / 'frame.jpg'
        _extract_frame(frozen_source, float(frame_seconds), frame)
    else:
        frame = frozen_source
    request = {'task_id': task_id, 'kind': 'vlm_observation', 'provider': provider,
               'model': model_id, 'service': service,
               'source': str(frozen_source), 'source_kind': kind, 'observation_frame': str(frame),
               'frame_seconds': float(frame_seconds) if kind == 'video' else None,
               'prompt': prompt.strip(), 'temperature': float(temperature), 'num_predict': num_predict,
               'source_width': facts.get('width'), 'source_height': facts.get('height'),
               'source_duration_seconds': facts.get('duration_seconds')}
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return {'kind': 'vlm_observation', 'provider': provider, 'model': model_id, 'request': request,
            'request_path': str(request_path), 'output_dir': str(directory / 'result'), 'engine': 'vlm-observation'}


EXPECTED_KEYS = {'task_id', 'kind', 'provider', 'model', 'service', 'source', 'source_kind', 'observation_frame',
                 'frame_seconds', 'prompt', 'temperature', 'num_predict', 'source_width', 'source_height',
                 'source_duration_seconds'}


def frozen_request(payload: dict) -> tuple[dict, Path]:
    request = payload.get('request')
    request_path = Path(str(payload.get('request_path') or ''))
    if not request_path.is_absolute() or not isinstance(request, dict):
        raise ValueError('观察冻结请求无效')
    if json.loads(request_path.read_text(encoding='utf-8')) != request:
        raise ValueError('观察请求快照与任务不一致')
    if set(request) != EXPECTED_KEYS or request['kind'] != 'vlm_observation':
        raise ValueError('观察冻结请求字段无效')
    directory = request_path.parent.resolve()
    if request['task_id'] != directory.name:
        raise ValueError('观察任务目录不一致')
    if Path(payload['output_dir']).resolve() != directory / 'result':
        raise ValueError('观察输出目录不一致')
    for key in ('source', 'observation_frame'):
        path = Path(str(request[key]))
        if not path.is_absolute() or path.parent.resolve() != directory or not path.is_file():
            raise ValueError('观察输入不在冻结任务目录内')
    if request['source_kind'] not in {'image', 'video'} or not str(request['prompt']).strip():
        raise ValueError('观察请求内容无效')
    return request, directory / 'result'


def _frame_bytes(request: dict) -> tuple[bytes, str]:
    image_bytes = Path(request['observation_frame']).read_bytes()
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise VlmFailed('观察画面为空或超过 32 MiB')
    mime = 'image/png' if Path(request['observation_frame']).suffix.lower() in {'.png', '.webp'} else 'image/jpeg'
    return image_bytes, mime


def _invoke(request: dict, cancelled: Callable[[], bool]) -> dict:
    """Send the frozen frame to the frozen provider; the key is never stored in the request."""
    if request['provider'] == CLOUD_PROVIDER:
        return _invoke_cloud(request, cancelled)
    return _invoke_local(request, cancelled)


def _invoke_local(request: dict, cancelled: Callable[[], bool]) -> dict:
    image_bytes, _mime = _frame_bytes(request)
    body = {'model': request['model'], 'prompt': request['prompt'], 'images': [base64.b64encode(image_bytes).decode()],
            'stream': True, 'keep_alive': '0',
            'options': {'temperature': request['temperature'], 'num_predict': request['num_predict']}}
    pieces: list[str] = []
    final: dict[str, Any] = {}
    deadline = time.monotonic() + RUN_TIMEOUT
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream('POST', f"{request['service']}/api/generate", json=body) as response:
                if response.status_code != 200:
                    response.read()
                    raise VlmFailed(f'Ollama 拒绝观察请求（HTTP {response.status_code}）')
                for line in response.iter_lines():
                    if cancelled():
                        raise VlmStopped('观察任务已停止')
                    if time.monotonic() > deadline:
                        raise VlmFailed('观察生成超时，已停止读取本次响应')
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if isinstance(chunk.get('response'), str):
                        pieces.append(chunk['response'])
                    if chunk.get('done'):
                        final = chunk
                        break
    except (VlmStopped, VlmFailed):
        raise
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise VlmFailed(f'Ollama 观察请求失败: {exc}') from exc
    if not final:
        raise VlmFailed('Ollama 没有返回完整观察回执')
    observation = ''.join(pieces).strip()
    if not observation:
        raise VlmFailed('模型没有返回可显示的文字观察')
    return {'observation': observation, 'done_reason': str(final.get('done_reason') or ''),
            'eval_count': final.get('eval_count'), 'eval_duration_ns': final.get('eval_duration'),
            'total_duration_ns': final.get('total_duration'), 'created_at': str(final.get('created_at') or '')}


def _invoke_cloud(request: dict, cancelled: Callable[[], bool]) -> dict:
    """OpenAI-compatible chat/completions with the frame as a data URL. This leaves the machine."""
    image_bytes, mime = _frame_bytes(request)
    api_key = cloud_config()['api_key']
    if not api_key:
        raise VlmFailed('云端观察的 API Key 已不可用，本次没有发送任何画面')
    body = {'model': request['model'], 'stream': True, 'temperature': request['temperature'],
            'max_tokens': request['num_predict'],
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': request['prompt']},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,' + base64.b64encode(image_bytes).decode()}}]}]}
    pieces: list[str] = []
    usage: dict[str, Any] = {}
    completed = False
    deadline = time.monotonic() + RUN_TIMEOUT
    timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=10.0)
    try:
        # trust_env stays on for the external endpoint: a corporate/VPN proxy is legitimate here,
        # unlike the loopback Ollama case.
        with httpx.Client(timeout=timeout) as client:
            with client.stream('POST', f"{request['service']}/chat/completions", json=body,
                               headers={'Authorization': f'Bearer {api_key}'}) as response:
                if response.status_code != 200:
                    detail = response.read().decode('utf-8', errors='replace')[:300]
                    if response.status_code in {400, 451} and 'risk' in detail.lower():
                        raise VlmFailed(f'云端内容风控拒绝了这一帧，未生成观察。这类内容请改用本机观察：{detail}')
                    raise VlmFailed(f'云端观察请求失败（HTTP {response.status_code}）：{detail}')
                for line in response.iter_lines():
                    if cancelled():
                        raise VlmStopped('观察任务已停止')
                    if time.monotonic() > deadline:
                        raise VlmFailed('云端观察超时，已停止读取本次响应')
                    if not line or not line.startswith('data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == '[DONE]':
                        completed = True
                        break
                    chunk = json.loads(payload)
                    if isinstance(chunk.get('usage'), dict):
                        usage = chunk['usage']
                    for choice in chunk.get('choices') or []:
                        content = (choice.get('delta') or {}).get('content')
                        if isinstance(content, str):
                            pieces.append(content)
    except (VlmStopped, VlmFailed):
        raise
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise VlmFailed(f'云端观察请求失败: {exc}') from exc
    if not completed:
        raise VlmFailed('云端观察没有返回完整回执')
    observation = ''.join(pieces).strip()
    if not observation:
        raise VlmFailed('云端模型没有返回可显示的文字观察')
    return {'observation': observation, 'done_reason': 'stop', 'eval_count': usage.get('completion_tokens'),
            'eval_duration_ns': None, 'total_duration_ns': None, 'created_at': '',
            'prompt_tokens': usage.get('prompt_tokens'), 'cloud_request': True}


def execute(payload: dict, cancelled: Callable[[], bool] = lambda: False) -> dict:
    request, output_dir = frozen_request(payload)
    if cancelled():
        raise VlmStopped('观察任务在启动前停止')
    if request['provider'] != CLOUD_PROVIDER:
        _ensure_service_online()
        if request['model'] not in _service_models():
            raise VlmFailed(f'Ollama 里没有 {request['model']}；请确认模型名称或改用已安装的视觉模型')
    answer = _invoke(request, cancelled)
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt = {**request, 'status': 'succeeded', **answer}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return read_receipt(payload)


def read_receipt(payload: dict) -> dict:
    request, output_dir = frozen_request(payload)
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('没有成功的观察回执')
    if any(receipt.get(key) != value for key, value in request.items()):
        raise ValueError('观察回执与冻结请求不一致')
    observation = receipt.get('observation')
    if not isinstance(observation, str) or not observation.strip():
        raise ValueError('观察回执没有文字内容')
    if receipt.get('model') != request['model']:
        raise ValueError('观察回执模型与冻结请求不一致')
    return receipt
