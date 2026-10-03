"""Mix an already converted vocal stem with a separate instrumental stem."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocal", type=Path, required=True)
    parser.add_argument("--instrumental", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--vocal-gain", type=float, default=0.85)
    parser.add_argument("--instrumental-gain", type=float, default=0.25)
    parser.add_argument("--target-peak", type=float, default=0.89)
    args = parser.parse_args()
    vocal, vocal_rate = sf.read(args.vocal, dtype="float32", always_2d=True)
    instrumental, instrumental_rate = sf.read(args.instrumental, dtype="float32", always_2d=True)
    if vocal_rate != instrumental_rate:
        raise ValueError(f"Sample-rate mismatch: {vocal_rate} != {instrumental_rate}")
    count = min(len(vocal), len(instrumental))
    if count <= 0:
        raise ValueError("Vocal and instrumental stems must contain audio")
    vocal_mono = vocal[:count].mean(axis=1)
    mix = instrumental[:count] * args.instrumental_gain + vocal_mono[:, None] * args.vocal_gain
    peak_before = float(np.max(np.abs(mix)))
    if peak_before > args.target_peak:
        mix *= args.target_peak / peak_before
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, mix, vocal_rate, subtype="PCM_16")
    result = {
        "status": "candidate_awaiting_listening",
        "vocal": str(args.vocal),
        "instrumental": str(args.instrumental),
        "output": str(args.output),
        "parameters": {
            "vocal_gain": args.vocal_gain,
            "instrumental_gain": args.instrumental_gain,
            "target_peak": args.target_peak,
        },
        "audio": {
            "sample_rate": vocal_rate,
            "channels": int(mix.shape[1]),
            "duration_seconds": round(count / vocal_rate, 4),
            "peak_before_limiter": round(peak_before, 6),
            "peak_after_limiter": round(float(np.max(np.abs(mix))), 6),
            "clipped_samples": int(np.sum(np.abs(mix) >= 0.999)),
        },
        "review_notes": [
            "B 版 H3 示例角色声线锚点；纯人声换音后再混入低比例伴奏。",
            "参考音频仅作为旋律、节奏和伴奏来源，不是交付音频。",
            "候选仍需人工完整听审，不能自动写入 delivery_master。",
        ],
    }
    args.manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
