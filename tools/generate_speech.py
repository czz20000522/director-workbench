"""Fixed IndexTTS adapter, launched with the existing IndexTTS Python environment."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths

TTS_ROOT = paths.SHARED_ROOT / 'tools/index-tts'
MODEL_ROOT = paths.SHARED_ROOT / 'models/IndexTTS-2.5'


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(request, dict):
        raise ValueError('声音任务必须是 JSON 对象')
    if not {'task_id', 'text', 'voice_reference'} <= set(request) or set(request) - {'task_id', 'text', 'voice_reference', 'emotion'}:
        raise ValueError('声音任务只接受 task_id、text、voice_reference 与可选的 emotion')
    for key in ('task_id', 'text', 'voice_reference'):
        if not isinstance(request[key], str) or not request[key].strip():
            raise ValueError(f'{key} 不能为空')
    if len(request['text']) > 2000:
        raise ValueError('单次台词不能超过 2000 字符')
    reference = Path(request['voice_reference'])
    if not reference.is_absolute() or not reference.is_file():
        raise ValueError('音色参考必须是已存在的绝对文件路径')
    request.setdefault('emotion', {'mode': 'none'})
    if request['emotion'].get('mode') == 'reference':
        emotion_path = Path(str(request['emotion'].get('reference') or ''))
        if not emotion_path.is_absolute() or not emotion_path.is_file():
            raise ValueError('情绪参考必须是已存在的绝对文件路径')
        request['emotion'] = {**request['emotion'], 'reference': str(emotion_path)}
    return request


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

    print('loading_model', flush=True)
    model = IndexTTS2(cfg_path=str(MODEL_ROOT / 'config.yaml'), model_dir=str(MODEL_ROOT),
                      use_bf16=True, use_cuda_kernel=False, use_torch_compile=False,
                      use_qwen_emo=bool(request['emotion'].get('use_qwen_emo')))
    print('generating', flush=True)
    output = output_dir / 'speech.wav'
    model.infer(spk_audio_prompt=request['voice_reference'], text=request['text'], lang='ZH',
                output_path=str(output), verbose=False, **infer_arguments(request['emotion']))
    info = sf.info(output)
    if info.frames <= 0 or info.samplerate <= 0:
        raise ValueError('声音执行器未生成有效音频')
    receipt = {**request, 'status': 'succeeded', 'output': str(output.resolve()),
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
