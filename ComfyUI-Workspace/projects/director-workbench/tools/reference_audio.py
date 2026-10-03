from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch
import whisper


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL_ROOT = ROOT / "ComfyUI-Shared" / "tools" / "index-tts" / "models" / "whisper"
FFMPEG_DIR = ROOT / "ComfyUI-Shared" / "tools" / "index-tts"


def root_relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def transcribe(audio: Path, model_root: Path, device: str) -> dict[str, object]:
    if not audio.is_file():
        raise FileNotFoundError(audio)
    if not (model_root / "base.pt").is_file():
        raise FileNotFoundError(model_root / "base.pt")
    if not (FFMPEG_DIR / "ffmpeg.exe").is_file():
        raise FileNotFoundError(FFMPEG_DIR / "ffmpeg.exe")
    os.environ["PATH"] = str(FFMPEG_DIR) + os.pathsep + os.environ.get("PATH", "")
    model = whisper.load_model("base", device=device, download_root=str(model_root))
    result = model.transcribe(
        str(audio), language="zh", task="transcribe", fp16=device == "cuda", verbose=False
    )
    return {
        "schema_version": 1,
        "engine": "openai-whisper-base",
        "reliability": "candidate_only",
        "audio": root_relative(audio),
        "language": result.get("language"),
        "text": str(result.get("text", "")).strip(),
        "segments": [
            {
                "start_seconds": round(float(segment["start"]), 3),
                "end_seconds": round(float(segment["end"]), 3),
                "text": str(segment["text"]).strip(),
            }
            for segment in result.get("segments", [])
        ],
        "warnings": [
            "演唱、方言、强伴奏和叠加人声会显著降低通用 ASR 的字词准确率。",
            "时间边界和重复结构可作候选证据，歌词字词必须与画面字幕、原词或人工听写核对。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="为参考拆解生成带时间戳、必须人工复核的 ASR 候选")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()
    payload = transcribe(args.audio.resolve(), args.model_root.resolve(), args.device)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"segments": len(payload["segments"]), "reliability": payload["reliability"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
