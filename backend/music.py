"""ACE-Step 1.5 配乐：显式参数、冻结工作流与候选登记。

配乐是一条 ComfyUI 工作流任务；本模块只负责把用户明确填写的参数变成一张可复现的
api-graph，并把生成结果登记成音频候选。不会自动写进音轨，也不会自动采用。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import paths

CHECKPOINT = 'ace_step_1.5_turbo_aio.safetensors'
CHECKPOINT_PATH = paths.SHARED_ROOT / 'models/checkpoints' / CHECKPOINT
NODES = ('CheckpointLoaderSimple', 'TextEncodeAceStepAudio1.5', 'EmptyAceStep1.5LatentAudio',
         'ModelSamplingAuraFlow', 'KSampler', 'VAEDecodeAudio', 'SaveAudioMP3', 'SaveAudio',
         'ConditioningZeroOut')
KEYSCALES = tuple([f'{root} {quality}' for quality in ('major', 'minor')
                   for root in ('C', 'C#', 'Db', 'D', 'D#', 'Eb', 'E', 'F', 'F#', 'Gb', 'G', 'G#', 'Ab', 'A', 'A#', 'Bb', 'B')])
TIME_SIGNATURES = ('2', '3', '4', '6')
LANGUAGES = ('ar', 'az', 'bg', 'bn', 'ca', 'cs', 'da', 'de', 'el', 'en', 'es', 'fa', 'fi', 'fr', 'he', 'hi', 'hr', 'ht',
             'hu', 'id', 'is', 'it', 'ja', 'ko', 'la', 'lt', 'ms', 'ne', 'nl', 'no', 'pa', 'pl', 'pt', 'ro', 'ru', 'sa',
             'sk', 'sr', 'sv', 'sw', 'ta', 'te', 'th', 'tl', 'tr', 'uk', 'ur', 'vi', 'yue', 'zh', 'unknown')
MP3_QUALITIES = ('V0', '128k', '320k')
MAX_TAGS = 2000
MAX_LYRICS = 4000
MIN_DURATION = 1.0
MAX_DURATION = 300.0
AUDIO_SUFFIXES = ('.mp3', '.flac', '.wav')


def capability(info: dict[str, Any]) -> dict[str, Any]:
    """Report node and checkpoint readiness; a present file is not an idle GPU."""
    missing_nodes = [name for name in NODES if name not in info]
    missing_models = [] if CHECKPOINT_PATH.is_file() else [f'checkpoints/{CHECKPOINT}']
    return {'available': not missing_nodes and not missing_models, 'engine': 'ACE-Step 1.5 Turbo',
            'id': 'ace-step-1.5', 'missing_nodes': missing_nodes, 'missing_models': missing_models,
            'keyscales': list(KEYSCALES), 'timesignatures': list(TIME_SIGNATURES), 'languages': list(LANGUAGES),
            'mp3_qualities': list(MP3_QUALITIES), 'duration_seconds': [MIN_DURATION, MAX_DURATION],
            'note': '参数全部由你显式指定；留空歌词即器乐。输出是新候选，不自动接入音轨'}


def _number(value: Any, label: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label}须在 {low} 至 {high} 之间')
    return float(value)


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}须为 {low} 至 {high} 的整数')
    return value


def validate(params: dict[str, Any]) -> dict[str, Any]:
    """Freeze every ACE-Step input the way the node exposes it; nothing is inferred."""
    tags = str(params.get('tags') or '').strip()
    if not tags or len(tags) > MAX_TAGS:
        raise ValueError(f'风格标签须为 1 至 {MAX_TAGS} 字符')
    lyrics = str(params.get('lyrics') or '')
    if len(lyrics) > MAX_LYRICS:
        raise ValueError(f'歌词不超过 {MAX_LYRICS} 字符；留空即器乐')
    duration = _number(params.get('duration_seconds'), '配乐时长（秒）', MIN_DURATION, MAX_DURATION)
    output_format = params.get('format') or 'mp3'
    if output_format not in {'mp3', 'flac'}:
        raise ValueError('配乐输出只能是 MP3 或 FLAC')
    quality = params.get('quality') or 'V0'
    if output_format == 'mp3' and quality not in MP3_QUALITIES:
        raise ValueError('MP3 质量只能是 V0、128k 或 320k')
    if output_format == 'flac':
        quality = 'V0'
    keyscale = str(params.get('keyscale') or '')
    if keyscale not in KEYSCALES:
        raise ValueError('调性必须是模型支持的标准调')
    timesignature = str(params.get('timesignature') or '')
    if timesignature not in TIME_SIGNATURES:
        raise ValueError('拍号只能是 2、3、4 或 6')
    language = str(params.get('language') or '')
    if language not in LANGUAGES:
        raise ValueError('歌词语言不在模型支持列表内')
    return {'tags': tags, 'lyrics': lyrics, 'duration_seconds': duration, 'bpm': _integer(params.get('bpm'), 'BPM', 10, 300),
            'keyscale': keyscale, 'timesignature': timesignature, 'language': language,
            'seed': _integer(params.get('seed'), '随机编号', 0, 2**32 - 1), 'steps': _integer(params.get('steps'), '采样步数', 1, 60),
            'cfg': _number(params.get('cfg'), 'CFG', 0, 10), 'shift': _number(params.get('shift'), 'Flow shift', 0, 20),
            'generate_audio_codes': bool(params.get('generate_audio_codes', True)),
            'encode_cfg_scale': _number(params.get('encode_cfg_scale'), '歌词生成 CFG', 0, 100),
            'temperature': _number(params.get('temperature'), '歌词温度', 0, 2),
            'top_p': _number(params.get('top_p'), '歌词 top_p', 0, 2000),
            'top_k': _integer(params.get('top_k'), '歌词 top_k', 0, 100),
            'min_p': _number(params.get('min_p'), '歌词 min_p', 0, 1),
            'format': output_format, 'quality': quality}


def build_graph(frozen: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    """Mirror the official ACE-Step 1.5 AIO workflow with explicit values only."""
    encode = {'clip': ['loader', 1], 'tags': frozen['tags'], 'lyrics': frozen['lyrics'], 'seed': frozen['seed'],
              'bpm': frozen['bpm'], 'duration': frozen['duration_seconds'], 'timesignature': frozen['timesignature'],
              'language': frozen['language'], 'keyscale': frozen['keyscale'],
              'generate_audio_codes': frozen['generate_audio_codes'], 'cfg_scale': frozen['encode_cfg_scale'],
              'temperature': frozen['temperature'], 'top_p': frozen['top_p'], 'top_k': frozen['top_k'],
              'min_p': frozen['min_p']}
    graph: dict[str, Any] = {
        'loader': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': CHECKPOINT}},
        'encode': {'class_type': 'TextEncodeAceStepAudio1.5', 'inputs': encode},
        'negative': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['encode', 0]}},
        'guidance': {'class_type': 'ModelSamplingAuraFlow', 'inputs': {'model': ['loader', 0], 'shift': frozen['shift']}},
        'latent': {'class_type': 'EmptyAceStep1.5LatentAudio', 'inputs': {'seconds': frozen['duration_seconds'], 'batch_size': 1}},
        'sampler': {'class_type': 'KSampler', 'inputs': {'model': ['guidance', 0], 'positive': ['encode', 0],
                                                         'negative': ['negative', 0], 'latent_image': ['latent', 0],
                                                         'seed': frozen['seed'], 'steps': frozen['steps'], 'cfg': frozen['cfg'],
                                                         'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        'decode': {'class_type': 'VAEDecodeAudio', 'inputs': {'samples': ['sampler', 0], 'vae': ['loader', 2]}},
    }
    if frozen['format'] == 'mp3':
        graph['save'] = {'class_type': 'SaveAudioMP3', 'inputs': {'audio': ['decode', 0], 'filename_prefix': prefix,
                                                                  'quality': frozen['quality']}}
    else:
        graph['save'] = {'class_type': 'SaveAudio', 'inputs': {'audio': ['decode', 0], 'filename_prefix': prefix}}
    return graph


def candidate_output(done: dict[str, Any]) -> Path:
    """Take this task's single saved audio file, refusing anything outside the output root."""
    outputs = [value for value in (done.get('outputs') or []) if str(value).lower().endswith(AUDIO_SUFFIXES)]
    if len(outputs) != 1:
        raise ValueError(f'配乐任务应当只产出一个音频候选，实际 {len(outputs)} 个')
    from . import app as backend
    root = backend.OUTPUT_ROOT.resolve()
    path = (root / str(outputs[0])).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('配乐候选不在作品输出目录内或文件已消失')
    return path


def complete_music_task(task: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
    """Register the audio candidate only; adoption stays an explicit user action."""
    from . import app as backend
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.verify_task(task)
    task = backend.get_task(task['id']) or task
    if task.get('stop_requested') or task['status'] == 'stopped':
        backend.set_task(task['id'], status='stopped', result=json.dumps(done, ensure_ascii=False),
                         error='已停止；配乐结果不登记候选')
        return backend.get_task(task['id'])
    if done.get('status') != 'success':
        backend.set_task(task['id'], status='failed', result=json.dumps(done, ensure_ascii=False),
                         error='ComfyUI 报告配乐未成功执行')
        return backend.get_task(task['id'])
    output = candidate_output(done)
    frozen = task['payload'].get('music') or {}
    receipt = {**done, 'output': str(output), 'music': frozen}
    with backend.MATERIAL_REUSE_LOCK:
        manifest = backend.require_project_manifest(task['project_id'])
        assets = list(manifest.get('assets') or [])
        asset_id = f"music-{task['id']}"
        if not any(asset.get('id') == asset_id for asset in assets if isinstance(asset, dict)):
            duration = frozen.get('duration_seconds')
            assets.append({'id': asset_id, 'kind': 'audio', 'title': f"配乐 · {str(frozen.get('tags') or '')[:28]}",
                           'ready': True, 'subtitle': '配乐候选 · 待审核', 'origin': 'ACE-Step 1.5 Turbo',
                           'sources': {'A': backend.root_relative_path(output)}, 'start': 0, 'end': duration,
                           'music_task_id': task['id']})
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if task['project_id'] == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
    backend.set_task(task['id'], status='succeeded', stop_requested=0, error=None,
                     result=json.dumps(receipt, ensure_ascii=False))
    return backend.get_task(task['id']) or task
