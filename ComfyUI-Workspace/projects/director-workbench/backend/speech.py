"""Immutable speech inputs and verified receipts shared by task/API adapters."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import wave
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TTS_ROOT = ROOT / 'ComfyUI-Shared/tools/index-tts'
ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_speech.py'
MODEL_ROOT = ROOT / 'ComfyUI-Shared/models/IndexTTS-2.5'
FLASH_ROOT = ROOT / 'ComfyUI-Shared/tools/AuK'
FLASH_ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_auk_flash.py'


def capability() -> dict:
    models = ROOT / 'ComfyUI-Shared/models/AuK/ckpts'
    required = {'声音执行器': FLASH_ADAPTER, 'AuK Python 环境': FLASH_ROOT / '.venv/Scripts/python.exe',
                'AuK 程序': FLASH_ROOT / 'src/auk/infer/infer_auk.py'}
    for name in ('AuK-Flash/config.yaml', 'AuK-Flash/auk_flash.safetensors', 'AuK-Flash/vae.safetensors',
                 'Qwen2.5-Omni-3B/config.json', 'Qwen2.5-Omni-3B/preprocessor_config.json',
                 'Qwen2.5-Omni-3B/tokenizer.json', 'Qwen2.5-Omni-3B/tokenizer_config.json',
                 'Qwen2.5-Omni-3B/model.safetensors.index.json',
                 'Qwen2.5-Omni-3B/model-00001-of-00003.safetensors',
                 'Qwen2.5-Omni-3B/model-00002-of-00003.safetensors',
                 'Qwen2.5-Omni-3B/model-00003-of-00003.safetensors'):
        required[name] = models / name
    missing = [label for label, path in required.items() if not path.is_file()]
    return {'available': not missing, 'missing': missing, 'engine': 'AuK-Flash',
            'language': 'ZH', 'note': '文件就绪不等于显卡空闲或生成质量已确认'}


class SpeechStopped(RuntimeError):
    pass


class SpeechFailed(RuntimeError):
    """Execution ended definitively, so it no longer reserves model capacity."""


def execution_failure_message(log_path: Path, returncode: int) -> str:
    """Expose actionable known failures without dumping model logs into the UI."""
    try:
        with log_path.open('rb') as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 8192))
            tail = log.read().decode('utf-8', errors='replace')
    except OSError:
        tail = ''
    if 'Reference plus requested generation must not exceed 30 seconds' in tail:
        return '音色参考与请求时长合计超过 30 秒，请缩短参考音频或请求时长后重新生成。'
    if 'CUDA out of memory' in tail or 'torch.OutOfMemoryError' in tail:
        return '声音生成显存不足，请等待其他模型任务释放显存后再尝试。'
    if 'Invalid reference audio' in tail or 'soundfile.LibsndfileError:' in tail:
        return '音色参考无法解码或内容无效，请在素材区选择可正常播放的 WAV 音频。'
    return f'声音执行器退出码 {returncode}，详情见任务 execution.log'


def stop_owned_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if result.returncode and process.poll() is None:
            raise RuntimeError('无法确认声音进程树已停止，需人工核对')
    else:
        process.kill()
    process.wait(timeout=30)


def execute(payload: dict, cancelled: Callable[[], bool], *, timeout: float = 900) -> dict:
    if cancelled():
        raise SpeechStopped('声音任务在启动前停止')
    engine = payload.get('engine', 'indextts-2.5')
    if engine == 'auk-flash':
        runtime, adapter = FLASH_ROOT, FLASH_ADAPTER
    elif engine == 'indextts-2.5':
        runtime, adapter = TTS_ROOT, ADAPTER
    else:
        raise SpeechFailed(f'声音任务引擎尚未接入: {engine}；不会改用其他模型')
    if payload.get('request', {}).get('engine', 'indextts-2.5') != engine:
        raise SpeechFailed('声音任务引擎与冻结请求不一致')
    command = [str(runtime / '.venv/Scripts/python.exe'), '-u', str(adapter),
               '--request', payload['request_path'], '--output-dir', payload['output_dir']]
    log_path = Path(payload['request_path']).with_name('execution.log')
    with log_path.open('wb') as log:
        try:
            process = subprocess.Popen(command, cwd=runtime, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError as exc:
            raise SpeechFailed(f'声音执行器无法启动: {exc}') from exc
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled():
                    raise SpeechStopped('声音任务已停止')
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('声音生成超时')
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException as exc:
            stop_owned_process(process)
            if isinstance(exc, TimeoutError):
                raise SpeechFailed('声音生成超时，已停止本次进程') from exc
            raise
    if process.returncode != 0:
        raise SpeechFailed(execution_failure_message(log_path, process.returncode))
    return read_receipt(payload)


def prepare(task_id: str, text: str, reference: Path | None, snapshot_root: Path, *,
            engine: str = 'indextts-2.5', gen_seconds: float = 4.5, seed: int = 20260923,
            mode: str = 'reference', voice_description: str | None = None) -> dict:
    if engine not in {'indextts-2.5', 'auk-flash'}:
        raise ValueError('未知声音引擎')
    if engine == 'auk-flash':
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError('声音种子须为 32 位非负整数')
        if type(gen_seconds) not in (int, float) or not math.isfinite(gen_seconds) or not 0 < gen_seconds <= 30:
            raise ValueError('声音请求时长须大于 0 且不超过 30 秒')
    if not task_id or len(task_id) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('声音任务 ID 无效')
    if not text.strip() or len(text) > 2000:
        raise ValueError('台词长度须为 1 至 2000 字符')
    if mode == 'design':
        if engine != 'auk-flash' or reference is not None or not isinstance(voice_description, str) or not voice_description.strip() or len(voice_description) > 2000:
            raise ValueError('声音设计须使用 AuK、提供描述且不提供参考音频')
    elif mode == 'reference':
        if reference is None or voice_description is not None:
            raise ValueError('参考模式须提供音频且不提供设计描述')
        reference = reference.resolve()
        if not reference.is_file() or reference.suffix.lower() not in {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg'}:
            raise ValueError('音色参考不是可用音频文件')
    else:
        raise ValueError('未知声音模式')
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    request = {'task_id': task_id, 'text': text}
    if mode == 'design':
        request.update(mode='design', voice_description=voice_description)
    else:
        frozen = directory / ('voice-reference' + reference.suffix.lower())
        shutil.copy2(reference, frozen)
        request['voice_reference'] = str(frozen.resolve())
    if engine == 'auk-flash':
        request.update(engine=engine, parameters={'seed': seed, 'gen_seconds': gen_seconds,
                       'nfe': 4, 'cfg_strength': 0.0, 'dtype': 'bf16', 'cpu_offload': True})
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'speech', 'engine': engine, 'request_path': str(request_path.resolve()),
            'output_dir': str((directory / 'result').resolve()), 'request': request}


def read_receipt(payload: dict) -> dict:
    """Only accept this task's actual, decodable output; missing receipt stays unknown."""
    output_dir = Path(payload['output_dir']).resolve()
    receipt = json.loads((output_dir / 'receipt.json').read_text(encoding='utf-8'))
    if not isinstance(receipt, dict) or receipt.get('status') != 'succeeded':
        raise ValueError('声音任务没有成功完成回执')
    if any(receipt.get(key) != value for key, value in payload['request'].items()):
        raise ValueError('声音回执与冻结输入不匹配')
    output = (output_dir / 'speech.wav').resolve()
    if Path(str(receipt.get('output', ''))).resolve() != output:
        raise ValueError('声音回执输出位置不匹配')
    with wave.open(str(output), 'rb') as audio:
        frames, rate, channels = audio.getnframes(), audio.getframerate(), audio.getnchannels()
        if frames <= 0 or rate <= 0 or len(audio.readframes(frames)) != frames * channels * audio.getsampwidth():
            raise ValueError('声音输出为空或不完整')
    if receipt.get('sample_rate') != rate or receipt.get('channels') != channels:
        raise ValueError('声音回执格式与文件不匹配')
    duration = float(receipt.get('duration_seconds', -1))
    if not math.isfinite(duration) or abs(duration - frames / rate) > 1 / rate:
        raise ValueError('声音回执时长与文件不匹配')
    return receipt
