from __future__ import annotations

import json
import sys
from pathlib import Path

from tools import reference_semantic


def test_atomic_result_temporary_does_not_duplicate_long_result_name(tmp_path, monkeypatch):
    parent = tmp_path / ('d' * max(1, 190 - len(str(tmp_path)) - 1))
    output = parent / ('a' * 32 + '.receipt.json')
    original_write = Path.write_text
    written = []
    def write(path, *args, **kwargs):
        written.append(path)
        if len(str(path)) >= 260:
            raise FileNotFoundError('Legacy Windows MAX_PATH')
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', write)
    reference_semantic.write_atomic_json(output, {'status': 'succeeded'})
    assert json.loads(output.read_text(encoding='utf-8')) == {'status': 'succeeded'}
    assert len(written) == 1 and not written[0].exists()
    assert list(parent.iterdir()) == [output]


def test_synthesis_is_noninteractive_and_exits_after_one_answer(monkeypatch):
    from types import SimpleNamespace
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['stdin'] == reference_semantic.subprocess.DEVNULL
        assert kwargs['encoding'] == 'utf-8'
        return SimpleNamespace(returncode=0, stdout='{"artifacts": {}}', stderr='')
    monkeypatch.setattr(reference_semantic.subprocess, 'run', run)
    assert reference_semantic.run_synthesis({}, [{'segment_id': 'R01', 'observation': '花园'}]) == {'artifacts': {}}
    assert '--single-turn' in commands[0]
    assert '--no-display-prompt' in commands[0]
    assert '--simple-io' in commands[0]
    assert '--no-conversation' in commands[0]
    assert commands[0][0].endswith('llama-completion.exe')
    assert '-p' in commands[0]
    assert commands[0][commands[0].index('-p') + 1].endswith('<|im_start|>assistant\n<think>\n</think>\n')


def test_result_output_does_not_write_over_analysis(tmp_path, monkeypatch) -> None:
    analysis = tmp_path / "analysis.json"
    original = {"artifacts": {"story": {"summary": "人工保留"}}}
    analysis.write_text(json.dumps(original), encoding="utf-8")
    result_path = tmp_path / "results/candidate.json"
    result = {"artifacts": {"story": {"summary": "模型候选"}}}
    monkeypatch.setattr(reference_semantic, "run_analysis", lambda *args: result)
    monkeypatch.setattr(sys, "argv", ["reference_semantic", "--analysis", str(analysis), "--result-output", str(result_path)])
    assert reference_semantic.main() == 0
    assert json.loads(analysis.read_text(encoding="utf-8")) == original
    assert json.loads(result_path.read_text(encoding="utf-8")) == result


def test_parse_json_output_ignores_thinking_wrapper() -> None:
    result = reference_semantic.parse_json_output('<think>检查证据</think>\n{"timeline": [], "artifacts": {}}\n')
    assert result == {"timeline": [], "artifacts": {}}


def test_schema_requires_each_semantic_artifact() -> None:
    schema = reference_semantic.json_schema(["R01", "R02"])
    artifacts = schema["properties"]["artifacts"]
    assert artifacts["required"] == list(reference_semantic.ARTIFACT_IDS)
    assert artifacts["properties"]["story"]["properties"]["evidence"]["items"]["enum"] == ["R01", "R02"]


def test_visual_schema_requires_one_observation_per_supplied_frame() -> None:
    schema = reference_semantic.visual_schema(["R01", "R02", "R03"])
    timeline = schema["properties"]["timeline"]

    assert timeline["minItems"] == 3
    assert timeline["maxItems"] == 3
    assert timeline["items"]["properties"]["segment_id"]["enum"] == ["R01", "R02", "R03"]


def test_apply_result_marks_model_output_for_human_review() -> None:
    document = {
        "status": "needs_semantic_analysis",
        "timeline": [{"id": "R01", "inference": "等待分析", "status": "needs_review"}],
        "stages": [{"id": "semantic", "status": "ready"}, {"id": "review", "status": "waiting"}],
        "artifacts": {artifact_id: {"status": "pending_analysis", "summary": ""} for artifact_id in reference_semantic.ARTIFACT_IDS},
    }
    result = {
        "content_mode": "music_performance",
        "content_mode_reason": "同一表演者随音乐完成动作。",
        "timeline": [{"segment_id": "R01", "observation": "人物位于画面中央。"}],
        "artifacts": {
            artifact_id: {"summary": f"{artifact_id} 分析", "evidence": ["R01"], "confidence": 0.75}
            for artifact_id in reference_semantic.ARTIFACT_IDS
        },
    }

    reference_semantic.apply_result(document, result)

    assert document["status"] == "needs_human_review"
    assert document["timeline"][0]["inference"] == "人物位于画面中央。"
    assert document["artifacts"]["mechanisms"]["provenance"] == "model_inference"
    assert document["artifacts"]["production_seed"]["model"] == "Qwen3.8-27B-Uncensored-Q4_K_M"
    assert document["content_mode"] == "music_performance"
    assert document["stages"] == [{"id": "semantic", "status": "done"}, {"id": "review", "status": "ready"}]


def test_reanalysis_preserves_corrected_classification():
    document = {'content_mode': 'showcase', 'content_mode_reason': '仅角色问候',
                'content_mode_user_status': 'reviewed', 'content_mode_history': [{'content_mode': 'other'}]}
    reference_semantic.apply_result(document, {'content_mode': 'music_performance', 'content_mode_reason': '模型猜测'})
    assert document['content_mode'] == 'showcase'
    assert document['content_mode_reason'] == '仅角色问候'
    assert document['content_mode_user_status'] == 'reviewed'
    assert document['content_mode_history'] == [{'content_mode': 'other'}]
    assert document['content_mode_model_candidate'] == {'content_mode': 'music_performance', 'content_mode_reason': '模型猜测'}


def test_observation_checkpoint_only_resumes_matching_analysis(tmp_path: Path) -> None:
    frames = [("R01", tmp_path / "one.jpg"), ("R02", tmp_path / "two.jpg")]
    document = {
        "id": "ref-one",
        "source": {"path": "uploads/reference.mp4", "size_bytes": 123},
    }
    signature = reference_semantic.checkpoint_signature(document, frames)
    checkpoint = tmp_path / "semantic-observations.json"
    reference_semantic.save_observation_checkpoint(
        checkpoint,
        signature,
        [{"segment_id": "R01", "observation": "人物位于画面中央。"}],
    )

    assert reference_semantic.load_observation_checkpoint(checkpoint, signature, ["R01", "R02"]) == [
        {"segment_id": "R01", "observation": "人物位于画面中央。"}
    ]
    mismatched = {**signature, "analysis_id": "ref-two"}
    assert reference_semantic.load_observation_checkpoint(checkpoint, mismatched, ["R01", "R02"]) == []
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_rerun_keeps_human_corrections_and_stores_model_candidate() -> None:
    document = {
        "timeline": [{"id": "R01", "user_note": "人工判断", "status": "reviewed"}],
        "artifacts": {"story": {"summary": "人工结构", "user_status": "reviewed", "provenance": "user_confirmed", "confidence": 0.8}},
    }
    result = {"timeline": [{"segment_id": "R01", "observation": "新的模型观察"}], "artifacts": {"story": {"summary": "新的模型结构", "evidence": ["R01"], "confidence": 0.6}}}
    reference_semantic.apply_result(document, result)
    assert document["timeline"][0]["status"] == "reviewed"
    assert document["timeline"][0]["user_note"] == "人工判断"
    assert document["timeline"][0]["inference"] == "新的模型观察"
    artifact = document["artifacts"]["story"]
    assert artifact["summary"] == "人工结构"
    assert artifact["provenance"] == "user_confirmed"
    assert artifact["confidence"] == 0.8
    assert artifact["model_candidate"]["summary"] == "新的模型结构"


def test_run_analysis_reuses_complete_visual_checkpoint(tmp_path: Path, monkeypatch) -> None:
    frames = [("R01", tmp_path / "one.jpg"), ("R02", tmp_path / "two.jpg")]
    document = {
        "id": "ref-one",
        "source": {"path": "uploads/reference.mp4", "size_bytes": 123},
    }
    checkpoint = tmp_path / "semantic-observations.json"
    signature = reference_semantic.checkpoint_signature(document, frames)
    observations = [
        {"segment_id": "R01", "observation": "第一帧。"},
        {"segment_id": "R02", "observation": "第二帧。"},
    ]
    reference_semantic.save_observation_checkpoint(checkpoint, signature, observations)
    monkeypatch.setattr(reference_semantic, "pick_frames", lambda _document: frames)
    monkeypatch.setattr(reference_semantic.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        reference_semantic,
        "run_visual_batch",
        lambda _frames: (_ for _ in ()).throw(AssertionError("不应重复视觉分析")),
    )
    monkeypatch.setattr(
        reference_semantic,
        "run_synthesis",
        lambda _document, supplied: {
            "content_mode": "music_performance",
            "artifacts": {},
            "received": supplied,
        },
    )

    result = reference_semantic.run_analysis(document, checkpoint)

    assert result["received"] == observations
    assert result["analysis_route"]["frames_analyzed"] == 2
