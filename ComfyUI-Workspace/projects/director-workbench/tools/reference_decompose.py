from __future__ import annotations

import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import av


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FFMPEG = ROOT / "ComfyUI-Shared" / "tools" / "index-tts" / "ffmpeg.exe"
ASR_PYTHON = ROOT / "ComfyUI-Shared" / "tools" / "index-tts" / ".venv" / "Scripts" / "python.exe"
ASR_SCRIPT = Path(__file__).resolve().with_name("reference_audio.py")


def media_facts(source: Path) -> dict[str, Any]:
    with av.open(str(source)) as container:
        video = next(iter(container.streams.video), None)
        audio = next(iter(container.streams.audio), None)
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        if not duration and video and video.duration and video.time_base:
            duration = float(video.duration * video.time_base)
        fps = float(video.average_rate) if video and video.average_rate else 0.0
        return {
            "duration_seconds": round(duration, 3),
            "width": video.width if video else None,
            "height": video.height if video else None,
            "fps": round(fps, 3),
            "video_codec": video.codec_context.name if video else None,
            "audio_codec": audio.codec_context.name if audio else None,
            "audio_channels": audio.codec_context.channels if audio else 0,
            "sample_rate": audio.codec_context.sample_rate if audio else None,
            "has_video": video is not None,
            "has_audio": audio is not None,
        }


def run_ffmpeg(ffmpeg: Path, args: list[str]) -> None:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffmpeg 执行失败")


def extract_materials(source: Path, output: Path, facts: dict[str, Any], frame_count: int) -> tuple[list[Path], Path | None]:
    ffmpeg = DEFAULT_FFMPEG
    if not ffmpeg.is_file():
        raise RuntimeError(f"未找到 ffmpeg: {ffmpeg}")
    frames_dir = output / "frames"
    audio_dir = output / "audio"
    frames_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    duration = max(float(facts.get("duration_seconds") or 0), 0.1)
    interval = max(duration / max(frame_count, 1), 0.25)
    if facts.get("has_video"):
        run_ffmpeg(
            ffmpeg,
            ["-i", str(source), "-vf", f"fps=1/{interval:.6f},scale='min(960,iw)':-2", "-frames:v", str(frame_count), "-q:v", "3", str(frames_dir / "frame_%03d.jpg")],
        )
    audio_path: Path | None = None
    if facts.get("has_audio"):
        audio_path = audio_dir / "reference.wav"
        run_ffmpeg(ffmpeg, ["-i", str(source), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(audio_path)])
    return sorted(frames_dir.glob("frame_*.jpg")), audio_path


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def recommended_frame_count(duration_seconds: float) -> int:
    return max(12, min(36, math.ceil(max(duration_seconds, 1.0) / 6.0)))


def transcribe_audio(audio_path: Path, output: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not ASR_PYTHON.is_file() or not ASR_SCRIPT.is_file():
        return None, "本机 Whisper 运行环境未就绪"
    transcript_path = output / "transcript.raw.json"
    result = subprocess.run(
        [str(ASR_PYTHON), str(ASR_SCRIPT), "--audio", str(audio_path), "--output", str(transcript_path)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode:
        return None, result.stderr.strip() or "Whisper ASR 失败"
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"ASR 候选无法读取: {exc}"
    return payload if isinstance(payload, dict) else None, None


def pending_artifact(title: str, purpose: str) -> dict[str, Any]:
    return {
        "title": title,
        "status": "pending_analysis",
        "provenance": "model_inference",
        "summary": purpose,
        "evidence": [],
        "confidence": None,
        "user_status": "unreviewed",
    }


def build_analysis(project_id: str, analysis_id: str, source: Path, output: Path, original_name: str, frame_count: int) -> dict[str, Any]:
    facts = media_facts(source)
    if frame_count <= 0:
        frame_count = recommended_frame_count(float(facts.get("duration_seconds") or 0))
    frames, audio_path = extract_materials(source, output, facts, frame_count)
    transcript, transcript_error = transcribe_audio(audio_path, output) if audio_path else (None, None)
    duration = max(float(facts.get("duration_seconds") or 0), 0.0)
    segment_count = max(len(frames), 1)
    segment_duration = duration / segment_count if duration else 0.0
    timeline = []
    for index in range(segment_count):
        start = segment_duration * index
        end = duration if index == segment_count - 1 else segment_duration * (index + 1)
        frame = frames[min(index, len(frames) - 1)] if frames else None
        timeline.append(
            {
                "id": f"R{index + 1:02d}",
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "frame": rel(frame) if frame else None,
                "status": "needs_review",
                "facts": [f"分析帧按 {segment_duration:.2f} 秒等距抽取；不是镜头切点"] if frame else ["源素材没有可解码视频帧"],
                "inference": "等待多模态模型描述镜头、角色、动作和场景",
                "user_note": "",
            }
        )
    source_artifact = {
        "title": "源素材档案",
        "status": "ready",
        "provenance": "observable_fact",
        "summary": f"{facts.get('width') or '?'}x{facts.get('height') or '?'} · {duration:.2f}s · {facts.get('fps') or '?'}fps",
        "facts": facts,
        "evidence": [rel(source)],
        "confidence": 1.0,
        "user_status": "unreviewed",
    }
    artifacts = {
        "source": source_artifact,
        "timeline": {
            "title": "时间采样",
            "status": "ready",
            "provenance": "observable_fact",
            "summary": f"已抽取 {len(frames)} 个等距分析帧；它们用于覆盖全片，不代表真实镜头边界",
            "evidence": [item["frame"] for item in timeline if item.get("frame")],
            "confidence": 1.0,
            "user_status": "unreviewed",
        },
        "transcript": {
            "title": "台词与歌词候选",
            "status": "candidate_needs_review" if transcript else "pending_analysis",
            "provenance": "asr_candidate" if transcript else "model_inference",
            "summary": (
                f"Whisper Base 已生成 {len(transcript.get('segments', []))} 个时间戳片段；演唱、方言和伴奏会导致错字，禁止直接当作原词"
                if transcript else f"等待 ASR 转写，并与画面字幕和原词核对{f'：{transcript_error}' if transcript_error else ''}"
            ),
            "evidence": [rel(output / "transcript.raw.json")] if transcript else [],
            "confidence": 0.25 if transcript else None,
            "user_status": "unreviewed",
        },
        "audio": {
            "title": "声音结构",
            "status": "ready" if audio_path else "not_present",
            "provenance": "observable_fact",
            "summary": "已提取 48kHz 双声道分析母版" if audio_path else "源素材未检测到音轨",
            "evidence": [rel(audio_path)] if audio_path else [],
            "confidence": 1.0,
            "user_status": "unreviewed",
        },
        "story": pending_artifact("内容结构", "先判断叙事、音乐表演、舞蹈、展示或氛围模式，再分析相应的段落推进"),
        "shots": pending_artifact("分镜语言", "逐段识别景别、机位、运动、构图和转场"),
        "characters": pending_artifact("角色与造型", "归纳角色身份、外形、服装、声音和行为锚点"),
        "scenes": pending_artifact("场景设计", "归纳空间、时间、光线、道具和连续性约束"),
        "actions": pending_artifact("动作与表演", "拆分姿态、表情、手势、口型和节拍动作"),
        "mechanisms": pending_artifact("传播机制假设", "提出钩子、留存、反转、循环和评论触发假设；必须附证据与置信度"),
        "adaptation": pending_artifact("可迁移机制", "区分可复用结构、风格参考与不可直接复刻的具体内容"),
        "production_seed": pending_artifact("正向制作种子", "人工校订后生成角色、场景、声音和分镜草案"),
    }
    stages = [
        {"id": "import", "label": "参考素材导入", "status": "done", "executor": "script"},
        {"id": "probe", "label": "媒体探测", "status": "done", "executor": "script"},
        {"id": "split", "label": "确定性拆分", "status": "done", "executor": "script"},
        {"id": "semantic", "label": "多模态语义分析", "status": "ready", "executor": "qwen3.8-27b"},
        {"id": "review", "label": "人工校订", "status": "waiting", "executor": "human"},
        {"id": "package", "label": "结构化拆解包", "status": "waiting", "executor": "hybrid"},
        {"id": "production", "label": "转入正向生产", "status": "waiting", "executor": "human"},
    ]
    return {
        "schema_version": 1,
        "id": analysis_id,
        "project_id": project_id,
        "title": Path(original_name).stem,
        "status": "needs_semantic_analysis",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"original_name": original_name, "path": rel(source), "size_bytes": source.stat().st_size, **facts},
        "stages": stages,
        "timeline": timeline,
        "artifacts": artifacts,
        "model_route": {
            "deterministic": "PyAV + ffmpeg + Whisper Base candidate ASR",
            "primary_multimodal_analysis_and_prompt_writing": "Qwen3.8-27B-Uncensored-Q4_K_M",
            "optional_fast_visual_prepass": "Qwen3-VL-8B-Instruct-Q4_K_M",
            "audio_support_note": "当前 llama.cpp projector 不支持把音频直接交给 27B；ASR 是独立候选证据",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把参考音视频拆成可审阅的导演工作台分析包")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--analysis-id", required=True)
    parser.add_argument('--task-id')
    parser.add_argument('--completion-receipt', type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--original-name", required=True)
    parser.add_argument("--frames", type=int, default=0, help="0 表示按时长自动选择 12-36 个分析帧")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    frame_count = 0 if args.frames <= 0 else max(4, min(args.frames, 36))
    document = build_analysis(args.project_id, args.analysis_id, args.source.resolve(), args.output.resolve(), args.original_name, frame_count)
    (args.output / "analysis.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.completion_receipt:
        if not args.task_id:
            raise ValueError('completion receipt requires task id')
        receipt = {'task_id': args.task_id, 'source': str(args.source.resolve()),
                   'analysis_path': str((args.output / 'analysis.json').resolve()), 'status': 'succeeded'}
        args.completion_receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.completion_receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt), encoding='utf-8')
        temporary.replace(args.completion_receipt)
    print(json.dumps(document, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
