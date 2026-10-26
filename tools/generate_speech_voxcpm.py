"""VoxCPM2 single-line adapter for the workbench's 配音 task.

One `generate` call per task. Timbre comes either from a reference clip (cloning)
or from a natural-language description written straight into the text prefix, which
is how VoxCPM creates a brand-new voice. Delivery instructions are merged into that
same prefix because the model takes exactly one parenthesised description.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths

VOXCPM_ROOT = paths.VOXCPM_ROOT
MAX_TEXT = 2000


def build_prefix(request: dict) -> str:
    parts = []
    if request['mode'] == 'design':
        parts.append(request['voice_description'])
    emotion = request.get('emotion') or {}
    if emotion.get('mode') == 'text':
        parts.append(emotion['text'])
    return '，'.join(part.strip(' 。；;，,') for part in parts if part.strip())


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(request, dict):
        raise ValueError('声音任务必须是 JSON 对象')
    allowed = {'task_id', 'text', 'mode', 'emotion', 'engine', 'parameters', 'voice_reference', 'voice_description'}
    if not {'task_id', 'text', 'mode', 'emotion', 'engine', 'parameters'} <= set(request) or set(request) - allowed:
        raise ValueError('声音任务字段与 VoxCPM2 冻结请求不一致')
    if request['engine'] != 'voxcpm-2':
        raise ValueError('该执行器只接受 voxcpm-2 引擎')
    if request['mode'] == 'reference':
        reference = Path(str(request.get('voice_reference') or ''))
        if not reference.is_absolute() or not reference.is_file():
            raise ValueError('音色参考必须是已存在的绝对文件路径')
        request['voice_reference'] = str(reference)
    elif request['mode'] == 'design':
        if 'voice_reference' in request or not str(request.get('voice_description') or '').strip():
            raise ValueError('音色设计模式只提供文字描述')
    else:
        raise ValueError('未知声音模式')
    if not isinstance(request['text'], str) or not request['text'].strip() or len(request['text']) > MAX_TEXT:
        raise ValueError(f'台词长度须为 1 至 {MAX_TEXT} 字符')
    if request['emotion'].get('mode') not in {'none', 'text'}:
        raise ValueError('VoxCPM2 只支持不控制情绪或用文本描述情绪')
    parameters = request['parameters']
    if not isinstance(parameters, dict) or set(parameters) != {'seed', 'inference_timesteps', 'cfg_value'}:
        raise ValueError('声音任务参数须为 seed、inference_timesteps、cfg_value')
    for name, low, high in (('seed', 0, 2**32 - 1), ('inference_timesteps', 1, 60)):
        value = parameters[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} 须为 {low} 至 {high} 的整数')
    cfg = parameters['cfg_value']
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 0 <= cfg <= 20:
        raise ValueError('cfg_value 须在 0 至 20 之间')
    return request


def generate(request_path: Path, output_dir: Path) -> dict:
    request = load_request(request_path)
    # Each task owns a fresh directory; a retry must inspect its receipt first.
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
    sys.path.insert(0, str(VOXCPM_ROOT / 'src'))
    from voxcpm import VoxCPM
    import soundfile as sf

    print('loading_model', flush=True)
    model = VoxCPM.from_pretrained(str(VOXCPM_ROOT / 'pretrained_models/VoxCPM2'), load_denoiser=False)
    parameters = request['parameters']
    prefix = build_prefix(request)
    text = f'({prefix}){request["text"]}' if prefix else request['text']
    print('generating', flush=True)
    output = output_dir / 'speech.wav'
    wav = model.generate(text=text, reference_wav_path=request.get('voice_reference'),
                         cfg_value=parameters['cfg_value'], inference_timesteps=parameters['inference_timesteps'],
                         seed=parameters['seed'])
    # PCM16 keeps the workbench's own `wave`-based receipt checks honest.
    sf.write(output, wav, model.tts_model.sample_rate, subtype='PCM_16')
    info = sf.info(output)
    if info.frames <= 0 or info.samplerate <= 0:
        raise ValueError('声音执行器未生成有效音频')
    receipt = {**request, 'status': 'succeeded', 'output': str(output.resolve()), 'timbre_prefix': prefix,
               'sample_rate': info.samplerate, 'channels': info.channels,
               'duration_seconds': info.frames / info.samplerate}
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.request, args.output_dir.resolve()), ensure_ascii=False))
