"""Separate a reference mix and master a Seed-VC vocal candidate for review.

This is a project-local adapter, not a delivery approval step. It keeps the
converted vocal and the mixed candidate separate so the director can compare
voice identity against the finished listening experience.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio


def separate(source: Path, weights: Path) -> tuple[np.ndarray, np.ndarray, int]:
    bundle = torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS
    model = bundle._model_factory_func()
    model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)
    mixture, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    audio = torch.from_numpy(mixture.T.copy()).unsqueeze(0).to(device)
    with torch.inference_mode():
        stems = model(audio)[0].detach().cpu().numpy()
    vocals = stems[3].T
    instrumental = stems[:3].sum(axis=0).T
    return vocals, instrumental, sample_rate


def master(
    source: Path,
    converted: Path,
    weights: Path,
    output_dir: Path,
    vocal_gain: float,
    instrumental_gain: float,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    vocals, instrumental, sample_rate = separate(source, weights)
    converted_audio, converted_rate = sf.read(converted, dtype="float32", always_2d=True)
    if converted_rate != sample_rate:
        raise ValueError(f"Sample-rate mismatch: {converted_rate} != {sample_rate}")
    count = min(len(vocals), len(instrumental), len(converted_audio))
    if count < sample_rate:
        raise ValueError("The smoke-test clip is shorter than one second")
    vocals = vocals[:count]
    instrumental = instrumental[:count]
    converted_mono = converted_audio[:count].mean(axis=1)
    mixed = instrumental * instrumental_gain + converted_mono[:, None] * vocal_gain
    peak_before_limiter = float(np.max(np.abs(mixed)))
    target_peak = 10 ** (-1.0 / 20.0)
    if peak_before_limiter > target_peak:
        mixed *= target_peak / peak_before_limiter
    mixed_path = output_dir / "lumei-hook-seedvc-v2-mixed-candidate.wav"
    source_vocals_path = output_dir / "reference-vocal-stem.wav"
    converted_vocals_path = output_dir / "lumei-hook-seedvc-v2-vocal-stem.wav"
    instrumental_path = output_dir / "reference-hook-v2-instrumental-stem.wav"
    sf.write(source_vocals_path, vocals, sample_rate, subtype="PCM_16")
    sf.write(converted_vocals_path, converted_mono, sample_rate, subtype="PCM_16")
    sf.write(instrumental_path, instrumental, sample_rate, subtype="PCM_16")
    sf.write(mixed_path, mixed, sample_rate, subtype="PCM_16")
    manifest = {
        "status": "candidate_awaiting_listening",
        "source_mix": str(source),
        "converted_vocal": str(converted),
        "demucs_checkpoint": str(weights),
        "outputs": {
            "source_vocal_stem": str(source_vocals_path),
            "converted_vocal_stem": str(converted_vocals_path),
            "instrumental_stem": str(instrumental_path),
            "mixed_candidate": str(mixed_path),
        },
        "parameters": {
            "vocal_gain": vocal_gain,
            "instrumental_gain": instrumental_gain,
            "target_peak_dbfs": -1.0,
        },
        "audio": {
            "sample_rate": sample_rate,
            "channels": int(mixed.shape[1]),
            "duration_seconds": round(count / sample_rate, 4),
            "peak_before_limiter": round(peak_before_limiter, 6),
            "peak_after_limiter": round(float(np.max(np.abs(mixed))), 6),
            "clipped_samples": int(np.sum(np.abs(mixed) >= 0.999)),
        },
        "review_notes": [
            "参考混音只用于保留旋律、伴奏和段落结构；它不是交付音频。",
            "混合候选仍需人工听审，不能自动写入 delivery_master。",
            "重点检查音色、唱感、伴奏平衡、齿音和呼音颤音。",
        ],
    }
    (output_dir / "lumei-hook-seedvc-v2-mixed-candidate.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--converted", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--vocal-gain", type=float, default=0.65)
    parser.add_argument("--instrumental-gain", type=float, default=0.85)
    args = parser.parse_args()
    print(json.dumps(master(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
