from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
LLAMA_MTMD = Path(r"D:\software\llama.cpp\llama-mtmd-cli.exe")
LLAMA_CLI = Path(r"D:\software\llama.cpp\llama-completion.exe")
MODEL_ROOT = ROOT / "ComfyUI-Shared" / "models" / "LLM" / "Qwen3.8-27B-Uncensored"
MODEL = MODEL_ROOT / "Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
MMPROJ = MODEL_ROOT / "mmproj-F16.gguf"
ARTIFACT_IDS = ("story", "shots", "characters", "scenes", "actions", "mechanisms", "adaptation", "production_seed")


def resolve_root_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    path.relative_to(ROOT.resolve())
    return path


CONTENT_MODES = ("narrative", "music_performance", "dance", "showcase", "mood", "technical", "other")


def pick_frames(document: dict[str, Any], maximum: int = 36) -> list[tuple[str, Path]]:
    candidates = [item for item in document.get("timeline", []) if isinstance(item, dict) and item.get("frame")]
    if len(candidates) <= maximum:
        selected = candidates
    else:
        indexes = sorted({round(index * (len(candidates) - 1) / (maximum - 1)) for index in range(maximum)})
        selected = [candidates[index] for index in indexes]
    return [(str(item.get("id")), resolve_root_path(str(item["frame"]))) for item in selected]


def artifact_definition(segment_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string", "enum": segment_ids}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["summary", "evidence", "confidence"],
        "additionalProperties": False,
    }


def visual_schema(segment_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "timeline": {
                "type": "array",
                "minItems": len(segment_ids),
                "maxItems": len(segment_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_id": {"type": "string", "enum": segment_ids},
                        "observation": {"type": "string"},
                    },
                    "required": ["segment_id", "observation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["timeline"],
        "additionalProperties": False,
    }


def json_schema(segment_ids: list[str]) -> dict[str, Any]:
    artifact = artifact_definition(segment_ids)
    return {
        "type": "object",
        "properties": {
            "content_mode": {"type": "string", "enum": list(CONTENT_MODES)},
            "content_mode_reason": {"type": "string"},
            "artifacts": {
                "type": "object",
                "properties": {artifact_id: artifact for artifact_id in ARTIFACT_IDS},
                "required": list(ARTIFACT_IDS),
                "additionalProperties": False,
            },
        },
        "required": ["content_mode", "content_mode_reason", "artifacts"],
        "additionalProperties": False,
    }


def parse_json_output(output: str) -> dict[str, Any]:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    value = json.loads(output[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def run_visual_batch(frames: list[tuple[str, Path]]) -> list[dict[str, str]]:
    segment_ids = [segment_id for segment_id, _ in frames]
    prompt = (
        "你是参考视频的逐帧记录员。输入图片按顺序对应 " + "、".join(segment_ids) + "。"
        "每张图必须各写一条 observation，只记录可见事实：人物数量和一致性、服装、场景、景别与机位、瞬间姿态或动作、表情、构图，以及画面文字。"
        "画面文字能确认多少写多少，看不清处明确写[不清]；不要猜歌词，不要把中式盘扣服装自动等同汉服，不要推断剧情、因果或爆火原因。"
        "只输出符合 schema 的 JSON。"
    )
    command = [
        str(LLAMA_MTMD), "-m", str(MODEL), "-mm", str(MMPROJ),
        "--image", ",".join(str(path) for _, path in frames),
        "-ngl", "55", "-c", "8192", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
        "--image-min-tokens", "1024", "--image-max-tokens", "1024", "--no-warmup", "--temp", "0.1", "-n", "1600",
        "--json-schema", json.dumps(visual_schema(segment_ids), ensure_ascii=False, separators=(",", ":")),
        "-p", prompt,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "27B 逐帧视觉分析失败")
    parsed = parse_json_output(result.stdout)
    values = parsed.get("timeline") if isinstance(parsed.get("timeline"), list) else []
    observations = [
        {"segment_id": str(item.get("segment_id")), "observation": str(item.get("observation", "")).strip()}
        for item in values
        if isinstance(item, dict) and item.get("segment_id") in segment_ids
    ]
    found = {item["segment_id"] for item in observations}
    missing = [segment_id for segment_id in segment_ids if segment_id not in found]
    if missing:
        raise RuntimeError(f"27B 未返回全部逐帧记录: {', '.join(missing)}")
    return observations


def transcript_candidate(document: dict[str, Any]) -> dict[str, Any] | None:
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    source_path = source.get("path")
    if not source_path:
        return None
    path = resolve_root_path(str(source_path)).parent.parent / "transcript.raw.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def checkpoint_signature(document: dict[str, Any], frames: list[tuple[str, Path]]) -> dict[str, Any]:
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    return {
        "analysis_id": str(document.get("id", "")),
        "source_path": str(source.get("path", "")),
        "source_size_bytes": source.get("size_bytes"),
        "frames": [{"segment_id": segment_id, "path": str(path)} for segment_id, path in frames],
    }


def load_observation_checkpoint(
    path: Path, signature: dict[str, Any], segment_ids: list[str]
) -> list[dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(value, dict) or value.get("signature") != signature:
        return []
    allowed = set(segment_ids)
    observations = [
        {"segment_id": str(item.get("segment_id")), "observation": str(item.get("observation", "")).strip()}
        for item in value.get("observations", [])
        if isinstance(item, dict)
        and item.get("segment_id") in allowed
        and str(item.get("observation", "")).strip()
    ]
    by_id = {item["segment_id"]: item for item in observations}
    return [by_id[segment_id] for segment_id in segment_ids if segment_id in by_id]


def save_observation_checkpoint(
    path: Path, signature: dict[str, Any], observations: list[dict[str, str]]
) -> None:
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "signature": signature,
        "observations": observations,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_synthesis(document: dict[str, Any], observations: list[dict[str, str]]) -> dict[str, Any]:
    segment_ids = [item["segment_id"] for item in observations]
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    packet = {
        "source_facts": {key: source.get(key) for key in ("duration_seconds", "width", "height", "fps", "has_audio")},
        "visual_observations": observations,
        "asr_candidate": transcript_candidate(document),
    }
    prompt = (
        "你是视频导演与内容分析师。下面是按时间顺序覆盖全片的视觉事实，以及一个可能严重错字的 ASR 候选。"
        "先在 narrative、music_performance、dance、showcase、mood、technical、other 中判断内容类型。"
        "没有明确音乐、演唱或舞蹈证据时，普通说话、口型开合或挥手问候不能归为音乐表演；单纯角色亮相优先考虑showcase。"
        "场景切换本身不构成剧情；如果是同一人物随歌曲表演，应按音乐表演分析视觉钩子、身体动作、节奏变化、场景/服装变化、峰值和结尾，不得虚构目标、阻力或身份转换。"
        "视觉字幕是画面证据，ASR 只用于发现时间结构和可能重复句，不得当作原词；冲突时明确需要人工核对。"
        "story.summary 对非叙事内容写内容结构和段落推进。mechanisms 只能写传播机制假设，不能宣称爆火因果。"
        "adaptation 区分可复用结构、可选改编表现和需要用户确认的角色/文案复用选择；不得推断素材权属或许可，也不得把改编建议写成禁止复用。"
        "production_seed 应给出画面、表演、镜头、声音与负面约束；本作角色未确定时列为待确认，不强制用户替换参考角色。"
        "每项 evidence 只能引用给定片段编号，confidence 为 0 到 1。只输出符合 schema 的 JSON。\n证据包："
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    )
    command = [
        str(LLAMA_CLI), "--no-conversation", "--single-turn", "--simple-io", "--no-display-prompt",
        "-m", str(MODEL), "-ngl", "55", "-c", "8192", "-fa", "on",
        "-ctk", "q8_0", "-ctv", "q8_0", "--no-warmup", "--temp", "0.1", "-n", "2600",
        "--json-schema", json.dumps(json_schema(segment_ids), ensure_ascii=False, separators=(",", ":")),
        "-p", "<|im_start|>user\n" + prompt + "<|im_end|>\n<|im_start|>assistant\n<think>\n</think>\n",
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=900, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "27B 结构综合失败")
    return parse_json_output(result.stdout)


def run_analysis(document: dict[str, Any], checkpoint_path: Path | None = None) -> dict[str, Any]:
    if not LLAMA_MTMD.is_file() or not LLAMA_CLI.is_file() or not MODEL.is_file() or not MMPROJ.is_file():
        raise FileNotFoundError("27B 主模型、视觉投影或 llama.cpp 运行器尚未就绪")
    frames = pick_frames(document)
    if not frames:
        raise ValueError("参考拆解没有可用于视觉分析的关键帧")
    segment_ids = [segment_id for segment_id, _ in frames]
    signature = checkpoint_signature(document, frames)
    observations = (
        load_observation_checkpoint(checkpoint_path, signature, segment_ids) if checkpoint_path else []
    )
    completed = {item["segment_id"] for item in observations}
    if completed:
        print(f"恢复 {len(completed)}/{len(frames)} 帧已保存的视觉观察", flush=True)
    for index in range(0, len(frames), 4):
        batch = frames[index : index + 4]
        if all(segment_id in completed for segment_id, _ in batch):
            continue
        fresh = run_visual_batch(batch)
        by_id = {item["segment_id"]: item for item in observations}
        by_id.update({item["segment_id"]: item for item in fresh})
        observations = [by_id[segment_id] for segment_id in segment_ids if segment_id in by_id]
        completed = set(by_id)
        if checkpoint_path:
            save_observation_checkpoint(checkpoint_path, signature, observations)
        print(f"已保存 {len(completed)}/{len(frames)} 帧视觉观察", flush=True)
    if len(observations) != len(frames):
        missing = [segment_id for segment_id in segment_ids if segment_id not in completed]
        raise RuntimeError(f"逐帧分析缺少观察: {', '.join(missing)}")
    synthesis = run_synthesis(document, observations)
    synthesis["timeline"] = observations
    synthesis["analysis_route"] = {
        "visual_batches": (len(frames) + 3) // 4,
        "frames_analyzed": len(frames),
        "audio": "Whisper Base candidate ASR; current 27B projector does not support audio input",
    }
    return synthesis


def apply_result(document: dict[str, Any], result: dict[str, Any]) -> None:
    observations = {
        str(item.get("segment_id")): str(item.get("observation", "")).strip()
        for item in result.get("timeline", [])
        if isinstance(item, dict)
    }
    for segment in document.get("timeline", []):
        if isinstance(segment, dict) and observations.get(str(segment.get("id"))):
            segment["inference"] = observations[str(segment["id"])]
            if segment.get("status") != "reviewed":
                segment["status"] = "model_analyzed"
    artifacts = document.get("artifacts") if isinstance(document.get("artifacts"), dict) else {}
    model_artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    for artifact_id in ARTIFACT_IDS:
        value = model_artifacts.get(artifact_id)
        target = artifacts.get(artifact_id)
        if not isinstance(value, dict) or not isinstance(target, dict):
            continue
        candidate = {
            "status": "ready",
            "provenance": "model_inference",
            "summary": str(value.get("summary", "")).strip(),
            "evidence": [str(item) for item in value.get("evidence", [])],
            "confidence": float(value.get("confidence", 0)),
            "user_status": "unreviewed",
            "model": "Qwen3.8-27B-Uncensored-Q4_K_M",
        }
        if target.get("user_status") == "reviewed":
            target["model_candidate"] = candidate
        else:
            target.update(candidate)
    classification = {'content_mode': str(result.get('content_mode', 'other')),
                      'content_mode_reason': str(result.get('content_mode_reason', '')).strip()}
    if document.get('content_mode_user_status') == 'reviewed':
        document['content_mode_model_candidate'] = classification
    else:
        document.update(classification)
        document['content_mode_user_status'] = 'unreviewed'
    document["semantic_route"] = result.get("analysis_route", {})
    for stage in document.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == "semantic":
            stage["status"] = "done"
        if isinstance(stage, dict) and stage.get("id") == "review":
            stage["status"] = "ready"
    document["status"] = "needs_human_review"


def write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the atomic sibling shorter than the result name: deep Windows
    # workspaces can fit the result but exceed MAX_PATH with both names joined.
    temporary = path.with_name(f'.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="用 27B 原生多模态模型生成参考素材语义拆解")
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--result-output", type=Path, help="仅保存模型候选，由工作台合并最新人工校订")
    parser.add_argument("--completion-receipt", type=Path)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.completion_receipt and (not args.result_output or not args.job_id):
        parser.error('completion receipt requires result output and job id')
    path = args.analysis.resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    result = run_analysis(document, path.with_name("semantic-observations.json"))
    if args.result_output:
        write_atomic_json(args.result_output, result)
        # run_analysis waits for every owned model subprocess. A receipt is
        # written only after that work and the candidate file have completed.
        if args.completion_receipt:
            write_atomic_json(args.completion_receipt, {'job_id': args.job_id, 'analysis_path': str(path),
                              'result_path': str(args.result_output.resolve()), 'status': 'succeeded'})
        print(json.dumps({"status": "candidate_ready", "artifact_count": len(result.get("artifacts", {}))}, ensure_ascii=False))
        return 0
    # A long analysis may overlap an editor save. Merge into the latest document.
    document = json.loads(path.read_text(encoding="utf-8"))
    apply_result(document, result)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": document["status"], "artifact_count": len(result.get("artifacts", {}))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
