import json

from backend import app as backend
from test_private_workbench import create, private_workbench
from test_production_presets import add_shot, preset_client


BASE = "/api/projects/preset-test/segments/S01/versions"


def generated_version(client, project, task_id, prompt, when):
    document, plan_path = backend.load_project_plan("preset-test")
    segment = document["segments"][0]
    segment["prompt"] = prompt
    snapshot = backend.segment_generation_snapshot(segment)
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    video = project / f"workspaces/preset-test/assets/{task_id}.mp4"
    video.write_bytes(b"video " + task_id.encode())
    payload = {"project_id": "preset-test", "segment_snapshot": snapshot,
               "execution_snapshot": {"prompt": prompt, "materials": {}}}
    result = {"status": "success", "outputs": [video.name], "published_outputs": [str(video)]}
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id,prompt_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "S01", "succeeded", when - 1, when, json.dumps(payload), json.dumps(result), "preset-test", task_id),
        )
        connection.commit()
    backend.record_segment_result("S01", task_id, "workflow.json", result, plan_path,
                                  task_id=task_id, payload=payload, generated_at=when)
    return video


def test_latest_generation_is_current_and_history_keeps_system_generation_time(preset_client):
    client, project = preset_client
    add_shot(client, project, "S01")
    long_prompt = "镜头动作与声音。" * 1500
    assert client.patch("/api/projects/preset-test/plan/segments/S01", json={"prompt": long_prompt}).status_code == 200
    old = generated_version(client, project, "first", long_prompt, 1_780_000_000.125)
    new = generated_version(client, project, "second", "改成另一处场景", 1_780_000_060.5)
    page = client.get(BASE, params={"limit": 1}).json()
    assert page["total"] == 2 and page["next_offset"] == 1
    assert page["items"][0]["task_id"] == "second" and page["items"][0]["is_current"]
    assert page["items"][0]["generated_at"] == "2026-05-28T20:27:40.500Z"
    older = client.get(BASE, params={"limit": 1, "offset": 1}).json()["items"][0]
    assert older["task_id"] == "first" and not older["is_current"]
    assert older["snapshot"]["prompt"] == long_prompt
    assert old.is_file() and new.is_file()
    document, _ = backend.load_project_plan("preset-test")
    assert document["segments"][0]["video"]["path"] == str(new)


def test_restore_keeps_original_generated_at_and_records_separate_restore_time(preset_client):
    client, project = preset_client
    add_shot(client, project, "S01")
    old = generated_version(client, project, "first", "旧版完整提示词", 1_780_000_000.125)
    generated_version(client, project, "second", "新版完整提示词", 1_780_000_060.5)
    document, _ = backend.load_project_plan("preset-test")
    revision = document["revision"]
    assert client.post(BASE + "/first/restore", json={"expected_plan_revision": revision - 1}).status_code == 409
    response = client.post(BASE + "/first/restore", json={"expected_plan_revision": revision})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["revision"] == revision + 1
    assert restored["segment"]["video"]["path"] == str(old)
    assert restored["segment"]["prompt"] == "旧版完整提示词"
    items = client.get(BASE).json()["items"]
    original = next(item for item in items if item["task_id"] == "first")
    assert original["generated_at"] == "2026-05-28T20:26:40.125Z"
    assert original["restored_at"] == restored["restored_at"]
    assert original["is_current"]
    assert next(item for item in items if item["task_id"] == "second")["restored_at"] is None


def test_missing_or_foreign_history_file_cannot_be_restored(preset_client):
    client, project = preset_client
    add_shot(client, project, "S01")
    old = generated_version(client, project, "first", "旧版", 1_780_000_000.125)
    generated_version(client, project, "second", "新版", 1_780_000_060.5)
    document, _ = backend.load_project_plan("preset-test")
    revision = document["revision"]
    assert client.post(BASE + "/unknown/restore", json={"expected_plan_revision": revision}).status_code == 404
    old.rename(old.with_name("unavailable.mp4"))
    assert client.post(BASE + "/first/restore", json={"expected_plan_revision": revision}).status_code == 409
    assert backend.load_project_plan("preset-test")[0]["revision"] == revision


def test_assembly_snapshot_uses_current_video_even_with_older_approved_review(preset_client):
    client, project = preset_client
    add_shot(client, project, "S01")
    old = generated_version(client, project, "first", "旧版", 1_780_000_000.125)
    new = generated_version(client, project, "second", "新版", 1_780_000_060.5)
    backend.persist_review(backend.ReviewRecordRequest(
        asset_id="S01", stage="sample", status="approved", candidate_ref=str(old), note="旧批准记录"
    ), "preset-test")
    builder = project / "tools/current_builder.py"
    builder.parent.mkdir(exist_ok=True)
    builder.write_text("def build_graph(plan, audio, prefix):\n    return {'video': plan['segments'][0]['video']['path']}\n", encoding="utf-8")
    manifest = backend.load_project_manifest("preset-test")
    manifest["assembly"] = {"builder": "tools/current_builder.py"}
    backend.save_project_manifest(manifest)
    graph = json.loads(backend.build_assembly_snapshot(manifest).read_text(encoding="utf-8"))
    assert graph["video"] == str(new)


def test_existing_successful_task_without_new_history_is_listed_as_partial(preset_client):
    client, project = preset_client
    add_shot(client, project, "S01")
    video = project / "workspaces/preset-test/assets/legacy.mp4"
    video.write_bytes(b"old successful task")
    plan, plan_path = backend.load_project_plan("preset-test")
    plan["segments"][0].update(video={"path": str(video)}, comfyui_task_id="legacy")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    payload = {"project_id": "preset-test", "execution_snapshot": {
        "prompt": "以前提交的提示词", "materials": {"first-frame": "first.png", "guide": "guide.wav"},
        "parameters": {"duration_seconds": 5},
    }}
    result = {"status": "success", "published_outputs": [str(video)]}
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id,prompt_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacy", "S01", "succeeded", 1_780_000_000, 1_780_000_060,
             json.dumps(payload), json.dumps(result), "preset-test", "legacy"),
        )
        connection.commit()
    item = client.get(BASE).json()["items"][0]
    assert item["task_id"] == "legacy" and item["is_current"]
    assert not item["snapshot_complete"]
    assert item["snapshot"]["prompt"] == "以前提交的提示词"
    assert item["snapshot"]["first_frame"] == "first.png"
    assert item["snapshot"]["audio_guide"] == "guide.wav"
    assert item["snapshot"]["duration_seconds"] == 5


def test_private_user_cannot_read_or_restore_another_users_versions(private_workbench):
    owner, other, _, _ = private_workbench
    project = create(owner)
    project_id = project["id"]
    assert owner.post(f"/api/projects/{project_id}/plan/segments", json={"segment_id": "S01", "duration_seconds": 5}).status_code == 200
    base = f"/api/projects/{project_id}/segments/S01/versions"
    assert other.get(base).status_code == 404
    assert other.post(base + "/task/restore", json={"expected_plan_revision": 0}).status_code == 404
