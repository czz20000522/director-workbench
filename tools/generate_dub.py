"""Multi-cue IndexTTS adapter: one model load, one WAV per explicitly timed cue."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths

TTS_ROOT = paths.SHARED_ROOT / 'tools/index-tts'
MODEL_ROOT = paths.SHARED_ROOT / 'models/IndexTTS-2.5'

MAX_CUES = 24
MAX_TEXT = 500


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(request, dict):
        raise ValueError('配音任务必须是 JSON 对象')
    if set(request) != {'task_id', 'kind', 'engine', 'source', 'voice_reference', 'cues', 'original_audio',
                        'original_gain', 'emotion', 'seed', 'inference_timesteps', 'cfg_value',
                        'source_duration_seconds', 'source_has_audio'} \
            or request['kind'] != 'anchor_dubbing':
        raise ValueError('配音任务字段与锚点配音冻结请求不一致')
    if request['engine'] != 'indextts-2.5':
        raise ValueError('该执行器只接受 indextts-2.5 引擎')
    task_id = request['task_id']
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError('task_id 不能为空')
    reference = Path(str(request['voice_reference'] or ''))
    if not reference.is_absolute() or not reference.is_file():
        raise ValueError('音色参考必须是已存在的绝对文件路径')
    cues = request['cues']
    if not isinstance(cues, list) or not 1 <= len(cues) <= MAX_CUES:
        raise ValueError(f'台词时间点须为 1 至 {MAX_CUES} 条')
    normalized = []
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict) or set(cue) != {'at_seconds', 'text', 'gain'}:
            raise ValueError('每条台词只接受 at_seconds、text 与 gain')
        at = cue['at_seconds']
        text = cue['text']
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError('台词开始时间必须是非负有限数值')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f'台词长度须为 1 至 {MAX_TEXT} 字符')
        normalized.append({'index': index, 'at_seconds': float(at), 'text': text})
    emotion_spec = request['emotion']
    if not isinstance(emotion_spec, dict) or emotion_spec.get('mode') not in {'none', 'reference', 'text', 'vector'}:
        raise ValueError('情绪设置与冻结请求不一致')
    if emotion_spec.get('mode') == 'reference':
        emotion_path = Path(str(emotion_spec.get('reference') or ''))
        if not emotion_path.is_absolute() or not emotion_path.is_file():
            raise ValueError('情绪参考必须是已存在的绝对文件路径')
        emotion_spec = {**emotion_spec, 'reference': str(emotion_path)}
    return {'task_id': task_id, 'voice_reference': str(reference), 'cues': normalized, 'emotion': emotion_spec}


def generate(request_path: Path, output_dir: Path) -> dict:
    request = load_request(request_path)
    # Each task owns a fresh directory; a retry must inspect its receipt first.
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(TTS_ROOT))
    from backend.emotion import infer_arguments
    from indextts.infer_v2_5 import IndexTTS2
    import soundfile as sf

    emotion_spec = request['emotion']
    print('loading_model', flush=True)
    model = IndexTTS2(cfg_path=str(MODEL_ROOT / 'config.yaml'), model_dir=str(MODEL_ROOT),
                      use_bf16=True, use_cuda_kernel=False, use_torch_compile=False,
                      use_qwen_emo=bool(emotion_spec.get('use_qwen_emo')))
    results = []
    for cue in request['cues']:
        output = output_dir / f"cue-{cue['index']:02d}.wav"
        print(f"generating cue {cue['index']}/{len(request['cues'])}", flush=True)
        model.infer(spk_audio_prompt=request['voice_reference'], text=cue['text'], lang='ZH',
                    output_path=str(output), verbose=False, **infer_arguments(emotion_spec))
        info = sf.info(output)
        if info.frames <= 0 or info.samplerate <= 0:
            raise ValueError(f"第 {cue['index']} 条台词未生成有效音频")
        results.append({**cue, 'output': str(output.resolve()), 'sample_rate': info.samplerate,
                        'channels': info.channels, 'duration_seconds': info.frames / info.samplerate})
    receipt = {'task_id': request['task_id'], 'voice_reference': request['voice_reference'],
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
