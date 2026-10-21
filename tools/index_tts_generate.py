"""Generate one dialogue WAV with the local IndexTTS-2.5 checkout.

This is the external speech stage for the workbench dialogue-shot pipeline.  It
never selects a voice: the caller passes an explicit reference WAV and text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', required=True)
    parser.add_argument('--text', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--language', default='Chinese')
    parser.add_argument('--duration-factor', type=float, default=1.0)
    parser.add_argument('--emotion', default='[]')
    parser.add_argument('--index-tts-home', default='')
    args = parser.parse_args()

    home_value = (args.index_tts_home or os.environ.get('DIRECTOR_INDEX_TTS_HOME', '')).strip()
    if not home_value:
        raise SystemExit('DIRECTOR_INDEX_TTS_HOME or --index-tts-home is required')
    home = Path(home_value).resolve()
    sys.path.insert(0, str(home))
    from indextts.infer_v2_5 import IndexTTS2

    reference = Path(args.reference).resolve()
    output = Path(args.output).resolve()
    if not reference.is_file():
        raise SystemExit(f'reference audio not found: {reference}')
    output.parent.mkdir(parents=True, exist_ok=True)
    emotion = json.loads(args.emotion or '[]')
    if emotion and (not isinstance(emotion, list) or len(emotion) != 8):
        raise SystemExit('emotion must be an 8-element list')
    language = {'Chinese': 'ZH', 'English': 'EN'}.get(args.language, args.language)
    started = time.time()
    tts = IndexTTS2(
        cfg_path=str(home / 'checkpoints' / 'config.yaml'),
        model_dir=str(home / 'checkpoints'),
        use_bf16=True,
    )
    tts.infer(
        spk_audio_prompt=str(reference),
        text=args.text,
        lang=language,
        output_path=str(output),
        duration_factor=float(args.duration_factor),
        emo_vector=emotion or None,
        verbose=True,
    )
    if not output.is_file() or output.stat().st_size <= 0:
        raise SystemExit(f'IndexTTS did not write: {output}')
    print(json.dumps({
        'status': 'ok',
        'output': str(output),
        'size_bytes': output.stat().st_size,
        'elapsed_seconds': round(time.time() - started, 3),
        'reference': str(reference),
        'text': args.text,
        'language': args.language,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
