#!/usr/bin/env python3
"""Generate fixed-speaker Chinese voice samples with a local Qwen3-TTS model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_jobs(jobs: Any) -> list[str]:
    if not isinstance(jobs, list) or not jobs:
        return ["jobs: 至少需要一个声音任务"]
    errors: list[str] = []
    seen_outputs: set[str] = set()
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            errors.append(f"jobs[{index}]: 必须是对象")
            continue
        if not str(job.get("speaker", "")).strip():
            errors.append(f"jobs[{index}].speaker: 缺少音色")
        if not str(job.get("text", "")).strip():
            errors.append(f"jobs[{index}].text: 缺少中文台词")
        output = str(job.get("output", ""))
        if not output.lower().endswith(".wav") or Path(output).name != output:
            errors.append(f"jobs[{index}].output: 必须是当前目录下的 wav 文件名")
        if output in seen_outputs:
            errors.append(f"jobs[{index}].output: 输出文件重复")
        seen_outputs.add(output)
    return errors


def generate_jobs(
    jobs: list[dict[str, Any]],
    *,
    model_path: str | Path,
    output_dir: str | Path,
    seed: int,
) -> dict[str, Any]:
    errors = validate_jobs(jobs)
    if errors:
        raise ValueError("; ".join(errors))

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    model = Qwen3TTSModel.from_pretrained(
        str(Path(model_path).resolve()),
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )

    results = []
    for index, job in enumerate(jobs):
        job_seed = seed + index
        torch.manual_seed(job_seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(job_seed)
        speaker = job["speaker"]
        waves, sample_rate = model.generate_custom_voice(
            text=job["text"],
            language="Chinese",
            speaker=speaker,
            do_sample=True,
        )
        output_path = destination / job["output"]
        sf.write(output_path, waves[0], sample_rate)
        results.append(
            {
                "speaker": speaker,
                "text": job["text"],
                "seed": job_seed,
                "sample_rate": sample_rate,
                "output": str(output_path),
            }
        )
    report = {"model": str(Path(model_path).resolve()), "results": results}
    (destination / "voice-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Generate consistent Chinese character voices")
    parser.add_argument("jobs", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--seed", type=int, default=20902000)
    args = parser.parse_args()
    try:
        jobs = json.loads(args.jobs.read_text(encoding="utf-8"))
        report = generate_jobs(jobs, model_path=args.model, output_dir=args.output_dir, seed=args.seed)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
