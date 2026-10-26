"""VoxCPM2 multi-cue adapter: one model load, one WAV per explicitly timed cue.

VoxCPM has no emotion-vector or emotion-reference blend. Its delivery control is a
natural-language prefix in the text, which is also how a brand-new timbre is created
without any reference audio — so the frozen `emotion` modes are mapped, not faked.
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
MAX_CUES = 24
MAX_TEXT = 500


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(request, dict):
        raise ValueError('配音任务必须是 JSON 对象')
    if set(request) != {'task_id', 'kind', 'source', 'voice_reference', 'voice_description', 'cues',
                        'original_audio', 'original_gain', 'emotion', 'engine', 'seed', 'inference_timesteps',
                        'cfg_value', 'source_duration_seconds', 'source_has_audio'} or request['kind'] != 'anchor_dubbing':
        raise ValueError('配音任务字段与锚点配音冻结请求不一致')
    if request['engine'] != 'voxcpm-2':
        raise ValueError('该执行器只接受 voxcpm-2 引擎')
    task_id = request['task_id']
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError('task_id 不能为空')
    emotion = request['emotion']
    if not isinstance(emotion, dict) or emotion.get('mode') not in {'none', 'text'}:
        raise ValueError('VoxCPM2 只支持不控制情绪或用文本描述情绪')
    reference = request['voice_reference']
    if bool(reference) == bool(request['voice_description']):
        raise ValueError('VoxCPM2 配音需要且只需要一种音色来源：参考音频或文字描述')
    if reference not in (None, ''):
        reference_path = Path(str(reference))
        if not reference_path.is_absolute() or not reference_path.is_file():
            raise ValueError('音色参考必须是已存在的绝对文件路径')
        reference = str(reference_path)
    else:
        reference = None
    for name, low, high in (('seed', 0, 2**32 - 1), ('inference_timesteps', 1, 60),):
        value = request[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} 须为 {low} 至 {high} 的整数')
    cfg = request['cfg_value']
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 0 <= cfg <= 20:
        raise ValueError('cfg_value 须在 0 至 20 之间')
    cues = request['cues']
    if not isinstance(cues, list) or not 1 <= len(cues) <= MAX_CUES:
        raise ValueError(f'台词时间点须为 1 至 {MAX_CUES} 条')
    normalized = []
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict) or set(cue) != {'at_seconds', 'text', 'gain'}:
            raise ValueError('每条台词只接受 at_seconds、text 与 gain')
        at, text = cue['at_seconds'], cue['text']
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError('台词开始时间必须是非负有限数值')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f'台词长度须为 1 至 {MAX_TEXT} 字符')
        normalized.append({'index': index, 'at_seconds': float(at), 'text': text})
    return {'task_id': task_id, 'voice_reference': reference, 'voice_description': request['voice_description'],
            'cues': normalized, 'emotion': emotion, 'seed': request['seed'],
            'inference_timesteps': request['inference_timesteps'], 'cfg_value': float(cfg)}


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
    sample_rate = model.tts_model.sample_rate
    # VoxCPM takes one parenthesised description, so timbre and delivery merge into it.
    parts = [value.strip(' 。；;，,') for value in (request.get('voice_description'),
             request['emotion'].get('text') if request['emotion']['mode'] == 'text' else '') if value and value.strip()]
    style = '，'.join(parts)
    results = []
    for cue in request['cues']:
        output = output_dir / f"cue-{cue['index']:02d}.wav"
        text = f'({style}){cue["text"]}' if style else cue['text']
        print(f"generating cue {cue['index']}/{len(request['cues'])}", flush=True)
        wav = model.generate(text=text, reference_wav_path=request['voice_reference'],
                             cfg_value=request['cfg_value'], inference_timesteps=request['inference_timesteps'],
                             seed=request['seed'] + cue['index'] - 1)
        # PCM16 keeps the workbench's own `wave`-based duration checks honest.
        sf.write(output, wav, sample_rate, subtype='PCM_16')
        info = sf.info(output)
        if info.frames <= 0 or info.samplerate <= 0:
            raise ValueError(f"第 {cue['index']} 条台词未生成有效音频")
        results.append({**cue, 'output': str(output.resolve()), 'sample_rate': info.samplerate,
                        'channels': info.channels, 'duration_seconds': info.frames / info.samplerate})
    receipt = {'task_id': request['task_id'], 'voice_reference': request['voice_reference'],
               'voice_description': request['voice_description'], 'timbre_prefix': style,
               'engine': 'voxcpm-2', 'seed': request['seed'], 'sample_rate': sample_rate,
               'cues': results, 'status': 'succeeded'}
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
