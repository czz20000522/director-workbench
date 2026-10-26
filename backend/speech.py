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

import paths

from . import emotion

TTS_ROOT = paths.SHARED_ROOT / 'tools/index-tts'
ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_speech.py'
MODEL_ROOT = paths.SHARED_ROOT / 'models/IndexTTS-2.5'
FLASH_ROOT = paths.SHARED_ROOT / 'tools/AuK'
FLASH_ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_auk_flash.py'
VOXCPM_ROOT = paths.VOXCPM_ROOT
VOXCPM_MODELS = VOXCPM_ROOT / 'pretrained_models/VoxCPM2'
VOXCPM_ADAPTER = Path(__file__).resolve().parents[1] / 'tools/generate_speech_voxcpm.py'
# 配音默认引擎。三条链路各自独立：能力探测报告解析到的目录，缺文件就明确不可用，绝不换引擎。
DEFAULT_ENGINE = 'voxcpm-2'
EMOTION_MODES = {'voxcpm-2': {'none': True, 'reference': False, 'text': True, 'vector': False}}


def _voxcpm_capability() -> dict:
    required = {'声音执行器': VOXCPM_ADAPTER, 'VoxCPM Python 环境': VOXCPM_ROOT / '.venv/Scripts/python.exe',
                'VoxCPM 程序': VOXCPM_ROOT / 'src/voxcpm/core.py', 'VoxCPM2 主模型': VOXCPM_MODELS / 'model.safetensors',
                'VoxCPM2 配置': VOXCPM_MODELS / 'config.json', 'VoxCPM2 声码器': VOXCPM_MODELS / 'audiovae.pth'}
    missing = [label for label, path in required.items() if not path.is_file()]
    return {'available': not missing, 'missing': missing, 'engine': 'VoxCPM2', 'id': 'voxcpm-2',
            'modes': ['reference', 'design'], 'timbre': 'design-or-reference',
            'emotion_modes': dict(EMOTION_MODES['voxcpm-2']), 'emotion_unsupported': ['reference', 'vector'],
            'root': str(VOXCPM_ROOT),
            'sample_rate': 48000, 'seed': True, 'language': 'ZH + 30 语言 + 9 中文方言',
            'note': '可用文字描述凭空造音色；有随机编号可复现。情绪只用文本描述，无情绪向量'}


def _auk_capability() -> dict:
    models = paths.SHARED_ROOT / 'models/AuK/ckpts'
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
    return {'available': not missing, 'missing': missing, 'engine': 'AuK-Flash', 'id': 'auk-flash',
            'modes': ['reference', 'design'], 'language': 'ZH',
            'note': '文件就绪不等于显卡空闲或生成质量已确认'}


def _index_capability() -> dict:
    required = {'声音执行器': ADAPTER, 'IndexTTS Python 环境': TTS_ROOT / '.venv/Scripts/python.exe',
                'IndexTTS 程序': TTS_ROOT / 'indextts/infer_v2_5.py',
                'IndexTTS 配置': MODEL_ROOT / 'config.yaml',
                'IndexTTS 主模型': MODEL_ROOT / 'gpt.pth',
                'IndexTTS 声码器': MODEL_ROOT / 'codec.pth',
                'IndexTTS 语义模型': MODEL_ROOT / 's2mel.pth',
                '音色统计': MODEL_ROOT / 'wav2vec2bert_stats.pt',
                '语义特征模型': MODEL_ROOT / 'hf_cache/w2v-bert-2.0/model.safetensors',
                '说话人编码器': MODEL_ROOT / 'hf_cache/campplus_cn_common.bin',
                '声码器配置': MODEL_ROOT / 'hf_cache/bigvgan/config.json',
                '声码器权重': MODEL_ROOT / 'hf_cache/bigvgan/bigvgan_generator.pt'}
    missing = [label for label, path in required.items() if not path.is_file()]
    text_ready = (MODEL_ROOT / 'qwen0.6bemo4-merge' / 'config.json').is_file()
    return {'available': not missing, 'missing': missing, 'engine': 'IndexTTS 2.5',
            'id': 'indextts-2.5', 'modes': ['reference'], 'language': 'ZH',
            'timbre': 'reference-only', 'emotion_modes': emotion.modes(text_engine_ready=text_ready),
            'emotions': list(emotion.EMOTIONS), 'sample_rate': 22050, 'seed': False, 'root': str(TTS_ROOT),
            'note': '仅参考音色模式；生成时不冻结时长与随机编号。情绪可选，音色仍只来自参考音频'}


def capability(engine: str = 'auk-flash') -> dict:
    if engine == 'auk-flash':
        return _auk_capability()
    if engine == 'indextts-2.5':
        return _index_capability()
    if engine == 'voxcpm-2':
        return _voxcpm_capability()
    raise ValueError('未知声音引擎')


def capabilities() -> dict[str, dict]:
    """Every engine, each usable only when explicitly selected. Default first."""
    return {'voxcpm-2': _voxcpm_capability(), 'auk-flash': _auk_capability(), 'indextts-2.5': _index_capability()}


def engine_label(engine: str | None) -> str:
    """The name a candidate records how it was made, so legacy payloads keep AuK."""
    return {'indextts-2.5': 'IndexTTS 2.5', 'voxcpm-2': 'VoxCPM2'}.get(engine or '', 'AuK-Flash')


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
    elif engine == 'voxcpm-2':
        runtime, adapter = VOXCPM_ROOT, VOXCPM_ADAPTER
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
            engine: str = DEFAULT_ENGINE, gen_seconds: float = 4.5, seed: int = 20260923,
            mode: str = 'reference', voice_description: str | None = None,
            emotion_spec: dict | None = None, inference_timesteps: int = 10,
            cfg_value: float = 2.0) -> dict:
    if engine not in {'indextts-2.5', 'auk-flash', 'voxcpm-2'}:
        raise ValueError('未知声音引擎')
    if engine == 'voxcpm-2':
        allowed = EMOTION_MODES['voxcpm-2']
        frozen_emotion = emotion.validate(emotion_spec, text_engine_ready=True)
        if not allowed.get(frozen_emotion['mode']):
            raise ValueError('VoxCPM2 的情绪只能用文本描述，或干脆不控制情绪')
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError('声音随机编号须为 32 位非负整数')
        if type(inference_timesteps) is not int or not 1 <= inference_timesteps <= 60:
            raise ValueError('推理步数须为 1 至 60 的整数')
        if type(cfg_value) not in (int, float) or not math.isfinite(cfg_value) or not 0 <= cfg_value <= 20:
            raise ValueError('引导系数须在 0 至 20 之间')
    elif engine == 'indextts-2.5':
        frozen_emotion = emotion.validate(emotion_spec, text_engine_ready=(MODEL_ROOT / 'qwen0.6bemo4-merge' / 'config.json').is_file())
    else:
        if emotion_spec not in (None, {}, {'mode': 'none'}):
            raise ValueError('AuK-Flash 暂不接收情绪设置，请改用 VoxCPM2')
        frozen_emotion = None
    if engine == 'auk-flash':
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError('声音随机编号须为 32 位非负整数')
        if type(gen_seconds) not in (int, float) or not math.isfinite(gen_seconds) or not 0 < gen_seconds <= 30:
            raise ValueError('声音请求时长须大于 0 且不超过 30 秒')
    if not task_id or len(task_id) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('声音任务 ID 无效')
    if not text.strip() or len(text) > 2000:
        raise ValueError('台词长度须为 1 至 2000 字符')
    if mode == 'design':
        if engine not in {'auk-flash', 'voxcpm-2'} or reference is not None or not isinstance(voice_description, str) or not voice_description.strip() or len(voice_description) > 2000:
            raise ValueError('声音设计须使用 VoxCPM2 或 AuK、提供描述且不提供参考音频')
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
    elif engine == 'voxcpm-2':
        # The vox adapter needs the mode recorded: it is the only engine whose
        # reference path clones a timbre instead of describing one.
        request.update(mode=mode, engine=engine, parameters={'seed': seed, 'inference_timesteps': inference_timesteps,
                                                             'cfg_value': float(cfg_value)})
        request['emotion'] = frozen_emotion
    elif frozen_emotion['mode'] == 'reference':
        request['emotion'] = emotion.freeze(frozen_emotion, directory)
    else:
        request['emotion'] = frozen_emotion
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
