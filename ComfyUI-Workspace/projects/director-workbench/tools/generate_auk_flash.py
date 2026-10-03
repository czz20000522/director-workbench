"""Foreground AuK-Flash worker. No downloads, service management, or implicit retries."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sys
import wave

TTS_ROOT = Path('D:/Comfy-Desktop/ComfyUI-Shared/tools/AuK')
MODELS = Path('D:/Comfy-Desktop/ComfyUI-Shared/models/AuK/ckpts')
MODEL_FILES = {'config': MODELS / 'AuK-Flash/config.yaml',
               'checkpoint': MODELS / 'AuK-Flash/auk_flash.safetensors',
               'vae': MODELS / 'AuK-Flash/vae.safetensors',
               'qwen': MODELS / 'Qwen2.5-Omni-3B'}
TIME_GRID = [0.0, 0.07612049579620361, 0.2928932309150696, 0.6173166036605835, 1.0]


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding='utf-8'))
    design = isinstance(request, dict) and request.get('mode') == 'design'
    expected = {'task_id', 'text', 'engine', 'parameters'} | ({'mode', 'voice_description'} if design else {'voice_reference'})
    if not isinstance(request, dict) or set(request) != expected:
        raise ValueError('Expected task_id, text, voice_reference, engine, parameters')
    if not isinstance(request['task_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request['task_id']):
        raise ValueError('Invalid task_id')
    if not isinstance(request['text'], str) or not request['text'].strip() or len(request['text']) > 2000:
        raise ValueError('Text must contain 1–2000 characters')
    if design:
        description = request['voice_description']
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            raise ValueError('voice_description must contain 1–2000 characters')
    else:
        ref = request['voice_reference']
        if not isinstance(ref, str) or not Path(ref).is_absolute() or not Path(ref).is_file():
            raise ValueError('voice_reference must be an existing absolute file')
    if request['engine'] != 'auk-flash':
        raise ValueError('Only auk-flash is supported')
    p = request['parameters']
    if not isinstance(p, dict) or set(p) != {'seed', 'gen_seconds', 'nfe', 'cfg_strength', 'dtype', 'cpu_offload'}:
        raise ValueError('All six frozen parameters are required')
    if type(p['seed']) is not int or not 0 <= p['seed'] <= 2**32 - 1:
        raise ValueError('seed must be an unsigned 32-bit integer')
    if type(p['gen_seconds']) not in (int, float) or not math.isfinite(p['gen_seconds']) or not 0 < p['gen_seconds'] <= 30:
        raise ValueError('gen_seconds must be finite and in (0, 30]')
    if type(p['nfe']) is not int or p['nfe'] != 4:
        raise ValueError('Flash requires nfe=4')
    if type(p['cfg_strength']) not in (int, float) or p['cfg_strength'] != 0:
        raise ValueError('Flash requires cfg_strength=0')
    if p['dtype'] != 'bf16' or p['cpu_offload'] is not True:
        raise ValueError('This local profile requires bf16 and cpu_offload=true')
    return request


@contextmanager
def protect_decoder_peaks(decoder, diagnostics: dict):
    """Scale the final waveform before AuK's irreversible decoder clamp."""
    import torch

    def protect(_module, _inputs, output):
        if not isinstance(output, torch.Tensor) or not output.numel() or not torch.isfinite(output).all():
            raise ValueError('Invalid pre-clamp decoder waveform')
        peak = float(output.detach().abs().max().item())
        gain = 0.99 / peak if peak >= 1.0 else 1.0
        diagnostics.setdefault('calls', []).append({
            'raw_peak': peak, 'gain': gain,
            'raw_boundary_samples': int((output.detach().abs() >= 1).sum().item()),
        })
        return output * gain if gain < 1.0 else output

    handle = decoder.conv_post.register_forward_hook(protect)
    try:
        yield
        if len(diagnostics.get('calls', [])) != 1:
            raise ValueError('Expected one final decoder waveform; inspect AuK compatibility')
    finally:
        handle.remove()


def build_messages(request: dict) -> list[dict]:
    """Use AuK pe.config.yaml's native Chinese Instruct TTS template."""
    if request.get('mode') == 'design':
        content = [{'type': 'text', 'text': '请基于下面的描述: '
                    + json.dumps(request['voice_description'], ensure_ascii=False)
                    + ',生成语音内容' + json.dumps(request['text'], ensure_ascii=False) + '.'}]
    else:
        content = [{'type': 'text', 'text': 'Say the following with the same voice: "' + request['text'] + '"'},
                   {'type': 'audio', 'audio': request['voice_reference']}]
    return [{'role': 'user', 'content': content}]


def infer(request: dict, diagnostics: dict | None = None):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(TTS_ROOT / 'src'))
    from auk.infer.infer_auk import AukInfer
    p = request['parameters']
    print('loading_model', flush=True)
    model = AukInfer(str(MODEL_FILES['config']), str(MODEL_FILES['checkpoint']),
                     device='cuda:0', dtype=p['dtype'], cpu_offload=p['cpu_offload'],
                     qwen_path=str(MODEL_FILES['qwen']))
    if not model.is_flash:
        raise ValueError('Installed configuration is not AuK-Flash')
    messages = build_messages(request)
    print('generating', flush=True)
    with protect_decoder_peaks(model.vae_model, diagnostics if diagnostics is not None else {}):
        audio, rate = model.generate(messages, seed=p['seed'], gen_seconds=p['gen_seconds'],
                                     nfe=p['nfe'], cfg_strength=p['cfg_strength'])
    return audio.detach().float().cpu().numpy(), rate


def generate(request_path: Path, output_dir: Path) -> dict:
    request = load_request(request_path)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)
    for name, path in MODEL_FILES.items():
        if not (path.is_dir() if name == 'qwen' else path.is_file()):
            raise FileNotFoundError(path)
    import numpy as np
    import soundfile as sf
    # Decode before reserving output or loading the GPU model.
    if request.get('mode') != 'design':
        reference, ref_rate = sf.read(request['voice_reference'], always_2d=True)
        if not reference.size or not np.isfinite(reference).all() or ref_rate <= 0:
            raise ValueError('Invalid reference audio')
        if len(reference) / ref_rate + request['parameters']['gen_seconds'] > 30:
            raise ValueError('Reference plus requested generation must not exceed 30 seconds')
    output_dir.mkdir(parents=True, exist_ok=False)
    decoder_diagnostics: dict = {}
    audio, rate = infer(request, decoder_diagnostics)
    audio = np.asarray(audio)
    if audio.ndim == 1:
        audio = audio[None, :]
    if audio.ndim != 2 or audio.shape[0] not in (1, 2) or audio.shape[1] == 0 or not np.isfinite(audio).all():
        raise ValueError('Invalid generated samples; expected finite channels-first audio')
    if type(rate) is not int or not 8000 <= rate <= 192000:
        raise ValueError('Invalid generated sample rate')
    # Attenuate the original float waveform before PCM quantization. Reducing
    # the volume of an already clipped WAV cannot recover its lost peaks.
    raw_peak = float(np.max(np.abs(audio)))
    raw_out_of_range = int(np.count_nonzero((audio < -1) | (audio >= 1)))
    raw_boundary_samples = int(np.count_nonzero(np.abs(audio) >= 1))
    pcm_gain = 0.99 / raw_peak if raw_peak >= 1.0 else 1.0
    if pcm_gain < 1.0:
        audio = audio * pcm_gain
    output = output_dir / 'speech.wav'
    sf.write(output, audio.T, rate, subtype='PCM_16', format='WAV')
    with wave.open(str(output), 'rb') as decoded:
        frames, channels = decoded.getnframes(), decoded.getnchannels()
        if frames != audio.shape[1] or channels != audio.shape[0] or decoded.getframerate() != rate or decoded.getsampwidth() != 2:
            raise ValueError('PCM output metadata mismatch')
        if len(decoded.readframes(frames)) != frames * channels * 2:
            raise ValueError('Truncated PCM output')
    receipt = {**request, 'status': 'succeeded', 'output': str(output),
               'sample_rate': rate, 'channels': channels, 'duration_seconds': frames / rate,
               'actual_parameters': {**request['parameters'], 't_grid': TIME_GRID, 'sway_sampling_coef': None},
               'model_files': {name: str(path) for name, path in MODEL_FILES.items()},
               'encoding': 'PCM_16',
               'decoder_peak_protection': decoder_diagnostics,
               'pcm_conversion': {'raw_peak': raw_peak, 'raw_out_of_range_samples': raw_out_of_range,
                                  'raw_boundary_samples': raw_boundary_samples,
                                  'source_saturation_possible': raw_boundary_samples > 0,
                                  'gain': pcm_gain, 'output_peak': float(np.max(np.abs(audio)))},
               'pcm_clipped_samples': int(np.count_nonzero((audio < -1) | (audio >= 1)))}
    if request.get('mode') == 'design':
        receipt['actual_instruction'] = build_messages(request)[0]['content'][0]['text']
        receipt['instruction_template'] = 'auk-pe-instruct-tts-zh-v1'
    temporary = output_dir / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(output_dir / 'receipt.json')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.request, args.output_dir), ensure_ascii=False, allow_nan=False))
