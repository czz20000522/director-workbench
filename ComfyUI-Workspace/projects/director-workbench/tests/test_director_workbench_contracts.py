from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend
from test_private_workbench import private_workbench, create as create_private


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        pass


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    project_root = tmp_path / "ComfyUI-Workspace" / "workbench"
    catalog = project_root / "projects"
    catalog.mkdir(parents=True)
    (catalog / "catalog.json").write_text('{"projects": []}', encoding="utf-8")
    for name, value in {
        "ROOT": tmp_path, "PROJECT": project_root, "PROJECTS_ROOT": catalog,
        "PROJECT_CATALOG_PATH": catalog / "catalog.json", "DIRECTOR_WORKSPACES_ROOT": project_root / "workspaces",
        "DB_PATH": tmp_path / "director.sqlite3", "INPUT_ROOT": tmp_path / "input",
        "OUTPUT_ROOT": tmp_path / "output", "RUNTIME": tmp_path / "runtime", "SNAPSHOT_ROOT": tmp_path / "snapshots",
        "PRIVATE_WORKSPACES": None,
    }.items():
        monkeypatch.setattr(backend, name, value)
    for name in ('CURRENT_PROJECT', 'CURRENT_PROJECT_ID', 'PLAN_PATH', 'MEDIA', 'TASK_TEMPLATES', 'PIPELINE_STAGES'):
        monkeypatch.setattr(backend, name, getattr(backend, name))
    monkeypatch.setattr(backend, "require_submission_capacity", lambda: {"online": True})
    monkeypatch.setattr(backend.threading, "Thread", _NoopThread)
    client = TestClient(backend.app)
    created = client.post('/api/projects/create', json={'series': '测试系列', 'title': '隔离作品', 'project_id': 'project-a'})
    assert created.status_code == 200, created.text
    assert created.json()['project']['id'] == 'project-a'
    return client


def _insert_task(project_id: str, asset_id: str, status: str = "stopped") -> str:
    task_id = f"{project_id}-{asset_id}-{status}"
    now = time.time()
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, asset_id, status, now, now, json.dumps({"asset_id": asset_id}), project_id),
        )
        connection.commit()
    return task_id


def test_tasks_and_resume_are_isolated_by_project(client: TestClient) -> None:
    own_id = _insert_task("project-a", "S01")
    other_id = _insert_task("project-b", "S01")

    response = client.get("/api/tasks")

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [own_id]
    assert client.get(f"/api/tasks/{other_id}").status_code == 404
    assert client.post(f"/api/tasks/{other_id}/resume").status_code == 409


def test_project_mutations_are_blocked_while_any_task_can_still_use_comfyui(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_task("project-a", "S01", "queued")

    assert client.post("/api/projects/select", json={"project_id": "project-b"}).status_code == 409
    assert client.post("/api/projects/create", json={"series": "新系列", "title": "新作品"}).status_code == 409
    assert client.post("/api/projects/import", json={"path": str(tmp_path / "missing")}).status_code == 409

    with backend.db() as connection:
        connection.execute("UPDATE tasks SET status = 'stopped'")
        connection.commit()
    monkeypatch.setattr(backend, "load_project_manifest", lambda project_id: {"id": project_id, "plan_path": "ComfyUI-Workspace/projects/director-workbench/does-not-exist.json"})
    monkeypatch.setattr(backend, "apply_project_manifest", lambda manifest: None)
    monkeypatch.setattr(backend, "public_project", lambda manifest=None: {"id": "project-b"})
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"projects": [], "default_project_id": "project-a"}), encoding="utf-8")
    monkeypatch.setattr(backend, "PROJECT_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(backend, "load_project_catalog", lambda: {"projects": [], "default_project_id": "project-a"})

    assert client.post("/api/projects/select", json={"project_id": "project-b"}).status_code == 200

    monkeypatch.setattr(backend, "ROOT", tmp_path)
    monkeypatch.setattr(backend, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(backend, "DIRECTOR_WORKSPACES_ROOT", tmp_path / "workspaces")
    backend.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "import-source"
    source.mkdir()
    monkeypatch.setattr(backend, "scan_import_directory", lambda directory, project_id: ({"id": project_id, "title": "导入作品"}, {"counts": {}}))
    monkeypatch.setattr(backend, "persist_project_manifest", lambda manifest, make_default=True: None)
    assert client.post("/api/projects/import", json={"path": str(source)}).status_code == 200
    assert client.post("/api/projects/create", json={"series": "新系列", "title": "新作品"}).status_code == 200


def test_review_records_survive_without_browser_storage(client: TestClient) -> None:
    saved = client.post(
        "/api/project-state/reviews",
        json={
            "asset_id": "S01",
            "stage": "sample",
            "status": "approved",
            "note": "表演可用",
            "adopted_variant": "B",
            "source": "director-ui",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1

    response = client.get("/api/project-state")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['readiness']['project_id'] == 'project-a'

    review = state["reviews"]["S01"]["sample"]
    assert review["status"] == "approved"
    assert review["adopted_variant"] == "B"
    assert review["revision"] == 1
    assert review["created_at"]


def test_creative_confirmation_persists_approval_checkpoints_atomically(client: TestClient) -> None:
    response = client.post(
        "/api/project-state/creative/confirm",
        json={
            "values": {"intent": "原创意图", "world": "原创世界", "character": "角色锚点", "voice": "甜妹声线"},
            "status": "approved",
            "source": "director-test",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["atomic"] is True
    response = client.get("/api/project-state")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['readiness']['segments'] == []
    assert state["creative"]["status"] == "approved"
    assert state["creative"]["revision"] == 1
    assert state["checkpoints"]["intent"]["status"] == "approved"
    assert state["checkpoints"]["character"]["status"] == "approved"
    assert state["checkpoints"]["intent"]["note"] == "创作设定 revision 1"


def test_legacy_creative_approved_request_uses_atomic_path(client: TestClient) -> None:
    response = client.post(
        "/api/project-state/creative",
        json={"values": {"intent": "兼容入口"}, "status": "approved", "source": "director-test"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["atomic"] is True
    response = client.get("/api/project-state")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["checkpoints"]["intent"]["status"] == "approved"


def test_guide_context_query_keeps_saved_progress_selection_and_user_permissions(private_workbench, monkeypatch):
    import asyncio
    import httpx
    from mcp import Client
    from mcp_server.server import create_server

    a, b, _, _ = private_workbench
    first = create_private(a)
    second = create_private(a)
    create_private(b)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('read-only guide consulted ComfyUI'))
    saved = a.patch('/api/settings/guide', json={
        'expected_revision': 0, 'project_id': second['id'], 'status': 'skipped', 'completed_steps': ['goal'],
    })
    assert saved.status_code == 200, saved.text
    viewed = a.get('/api/settings/guide', params={'project_id': first['id']})
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()['view_project_id'] == first['id']
    assert viewed.json()['project_id'] == second['id']
    assert viewed.json()['readiness']['project_id'] == first['id']
    assert a.get('/api/settings/guide').json() == saved.json()
    assert a.get('/api/project').json()['id'] == second['id']
    assert b.get('/api/settings/guide', params={'project_id': first['id']}).status_code == 404
    assert b.get('/api/settings/guide').json()['revision'] == 0

    async def verify():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app), session_token=a.headers['Authorization'][7:])
        async with Client(server) as agent:
            response = (await agent.call_tool('read_guide_settings', {'project_id': first['id']})).structured_content
            assert response['ok'] and response['data'] == viewed.json()
    asyncio.run(verify())


def test_prompt_override_is_written_to_frozen_api_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "prompt-node": {
                    "class_type": "MiniMaxH3ImageToVideo",
                    "inputs": {"prompt": "old prompt", "seed": 7},
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot_root = tmp_path / "snapshots"
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", snapshot_root)

    frozen = backend.freeze_task_payload(
        {"asset_id": "S01", "workflow": str(source), "prompt_override": "new prompt"},
        project_id="project-a",
    )

    snapshot = Path(frozen["workflow"])
    graph = json.loads(snapshot.read_text(encoding="utf-8"))
    assert snapshot != source
    assert graph["prompt-node"]["inputs"]["prompt"] == "new prompt"
    assert frozen["execution_snapshot"]["prompt"] == "new prompt"
    assert frozen["execution_snapshot"]["api_graph"] == str(snapshot)


def test_prompt_override_fails_when_workflow_has_no_supported_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}}}), encoding="utf-8")
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    with pytest.raises(ValueError, match="提示词"):
        backend.freeze_task_payload(
            {"asset_id": "S01", "workflow": str(source), "prompt_override": "must not be ignored"},
            project_id="project-a",
        )


def test_task_submission_persists_the_prompt_in_its_frozen_graph(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "prompt-node": {
                    "class_type": "MiniMaxH3ImageToVideo",
                    "inputs": {"prompt": "old prompt", "seed": 7},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(backend, "PLAN_PATH", tmp_path / "missing-plan.json")
    monkeypatch.setattr(backend, "TASK_TEMPLATES", {"S01": {"workflow": str(source)}})

    response = client.post("/api/tasks", json={"asset_id": "S01", "prompt": "edited prompt"})

    assert response.status_code == 200
    task = response.json()
    snapshot_path = Path(task["payload"]["execution_snapshot"]["api_graph"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    original = json.loads(source.read_text(encoding="utf-8"))
    assert task["project_id"] == "project-a"
    assert task["payload"]["execution_snapshot"]["prompt"] == "edited prompt"
    assert snapshot["prompt-node"]["inputs"]["prompt"] == "edited prompt"
    assert original["prompt-node"]["inputs"]["prompt"] == "old prompt"


def test_workflow_download_returns_json_with_filename(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    workflow = project_root / "workflow.json"
    project_root.mkdir()
    workflow.write_text('{"1": {}}', encoding="utf-8")
    monkeypatch.setattr(backend, "PROJECT", project_root)

    response = client.get("/workflow-file", params={"path": "workflow.json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert 'filename="workflow.json"' in response.headers["content-disposition"]
    assert response.json() == {"1": {}}


def test_segment_update_persists_audio_materials_and_recalculates_timeline(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "revision": 1,
                "segments": [
                    {"id": "S01", "duration_seconds": 5.0, "keyframes": {}, "audio": {}},
                    {"id": "S02", "duration_seconds": 5.0, "keyframes": {}, "audio": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        backend,
        "load_project_plan",
        lambda project_id: (json.loads(plan_path.read_text(encoding="utf-8")), plan_path),
    )
    monkeypatch.setattr(backend, "load_project_manifest", lambda project_id: {"pipeline": []})

    response = client.patch(
        "/api/projects/project-a/plan/segments/S01",
        json={
            "duration_seconds": 4.75,
            "first_frame": "workspaces/project-a/assets/image/S01.png",
            "audio_guide": "workspaces/project-a/assets/audio/guides/S01.wav",
        },
    )

    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["revision"] == 2
    assert plan["segments"][0]["audio"]["guide"].endswith("S01.wav")
    assert plan["segments"][0]["keyframes"]["first"].endswith("S01.png")
    assert plan["segments"][0]["end_seconds"] == 4.75
    assert plan["segments"][1]["start_seconds"] == 4.75


def test_pipeline_values_are_validated_and_applied_to_frozen_graph(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    guide = tmp_path / "guide.wav"
    for path in (first, last, guide):
        path.write_bytes(b"fixture")
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "114": {"class_type": "LoadImage", "inputs": {"image": "old-first.png"}},
                "last": {"class_type": "LoadImage", "inputs": {"image": "old-last.png"}},
                "prompt": {"class_type": "Video", "inputs": {"prompt": "old prompt"}},
                "guide": {"class_type": "LoadAudio", "inputs": {"audio": "old.wav"}},
                "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
                "duration": {"class_type": "PrimitiveFloat", "inputs": {"value": 5.0}},
                "turbo": {"class_type": "PrimitiveBoolean", "inputs": {"value": True}},
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"segments": [{"id": "S01", "workflow": str(source), "prompt": "plan prompt"}]}), encoding="utf-8")
    stage = {
        "id": "motion-generation",
        "title": "音频驱动视频",
        "backend": "ComfyUI 工作流",
        "execution": {"mode": "comfyui"},
        "input_specs": [
            {"id": "first-frame", "label": "首帧", "kind": "图片", "required": True, "control": "asset", "binding": {"node_id": "114", "input": "image"}},
            {"id": "last-frame", "label": "尾帧", "kind": "图片", "required": True, "control": "asset", "binding": {"node_id": "last", "input": "image"}},
            {"id": "prompt", "label": "提示词", "kind": "文本", "required": True, "control": "text", "binding": {"node_id": "prompt", "input": "prompt"}},
            {"id": "guide", "label": "引导音频", "kind": "音频", "required": True, "control": "asset", "binding": {"node_id": "guide", "input": "audio"}},
            {"id": "seed", "label": "种子", "kind": "参数", "required": True, "control": "number", "min": 0, "max": 100, "binding": {"node_id": "noise", "input": "noise_seed"}},
            {"id": "duration", "label": "时长", "kind": "参数", "required": True, "control": "number", "min": 1, "max": 10, "binding": {"node_id": "duration", "input": "value"}},
            {"id": "turbo", "label": "Turbo", "kind": "参数", "required": True, "control": "select", "options": ["enable", "disable"], "binding": {"node_id": "turbo", "input": "value", "value_map": {"enable": True, "disable": False}}},
        ],
    }
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(backend, "PLAN_PATH", plan)
    monkeypatch.setattr(backend, "TASK_TEMPLATES", {})
    monkeypatch.setattr(backend, "PIPELINE_STAGES", [stage])

    response = client.post(
        "/api/tasks",
        json={
            "asset_id": "S01",
            "pipeline_stage_id": "motion-generation",
            "pipeline_values": {
                "first-frame": str(first),
                "last-frame": str(last),
                "prompt": "edited and executed",
                "guide": str(guide),
                "seed": 42,
                "duration": 6.5,
                "turbo": "disable",
            },
        },
    )

    assert response.status_code == 200, response.text
    snapshot_info = response.json()["payload"]["execution_snapshot"]
    frozen = json.loads(Path(snapshot_info["api_graph"]).read_text(encoding="utf-8"))
    staged_first = backend.INPUT_ROOT / frozen["114"]["inputs"]["image"]
    assert staged_first.is_file() and staged_first.read_bytes() == first.read_bytes()
    assert (backend.INPUT_ROOT / frozen["last"]["inputs"]["image"]).read_bytes() == last.read_bytes()
    assert frozen["prompt"]["inputs"]["prompt"] == "edited and executed"
    assert (backend.INPUT_ROOT / frozen["guide"]["inputs"]["audio"]).read_bytes() == guide.read_bytes()
    assert frozen["noise"]["inputs"]["noise_seed"] == 42
    assert frozen["duration"]["inputs"]["value"] == 6.5
    assert frozen["turbo"]["inputs"]["value"] is False
    assert snapshot_info["prompt"] == "edited and executed"
    assert snapshot_info["parameters"] == {"seed": 42, "duration": 6.5, "turbo": "disable"}
    assert snapshot_info["materials"] == {"first-frame": str(first), "last-frame": str(last), "guide": str(guide)}
    assert len(snapshot_info["applied_bindings"]) == 7
    first.write_bytes(b'changed source after submission')
    assert staged_first.read_bytes() == b'fixture'
    assert json.loads(source.read_text(encoding="utf-8"))["prompt"]["inputs"]["prompt"] == "old prompt"


def test_comfyui_pipeline_rejects_an_unbound_operator_input(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"prompt": {"class_type": "Video", "inputs": {"prompt": "old"}}}), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"segments": [{"id": "S01", "workflow": str(source)}]}), encoding="utf-8")
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "PLAN_PATH", plan)
    monkeypatch.setattr(backend, "TASK_TEMPLATES", {})
    monkeypatch.setattr(
        backend,
        "PIPELINE_STAGES",
        [{"id": "motion-generation", "title": "视频", "backend": "ComfyUI 工作流", "execution": {"mode": "comfyui"}, "input_specs": [{"id": "prompt", "label": "提示词", "kind": "文本", "required": True, "control": "text"}]}],
    )

    response = client.post(
        "/api/tasks",
        json={"asset_id": "S01", "pipeline_stage_id": "motion-generation", "pipeline_values": {"prompt": "must execute"}},
    )

    assert response.status_code == 422
    assert "执行绑定" in response.json()["detail"]


def test_batch_freezes_each_segment_from_its_own_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    sources: dict[str, Path] = {}
    segments = []
    for asset_id, prompt in (("S01", "first prompt"), ("S02", "second prompt")):
        source = tmp_path / f"{asset_id}.json"
        source.write_text(json.dumps({"prompt": {"class_type": "Video", "inputs": {"prompt": prompt}}}), encoding="utf-8")
        sources[asset_id] = source
        segments.append({"id": asset_id, "workflow": str(source), "prompt": prompt})
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    stage = {
        "id": "motion-generation",
        "title": "视频",
        "backend": "ComfyUI 工作流",
        "execution": {"mode": "comfyui"},
        "input_specs": [{"id": "prompt", "label": "提示词", "kind": "文本", "required": True, "control": "text", "binding": {"node_id": "prompt", "input": "prompt"}}],
    }
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(backend, "PLAN_PATH", plan)
    monkeypatch.setattr(backend, "TASK_TEMPLATES", {})
    monkeypatch.setattr(backend, "PIPELINE_STAGES", [stage])

    response = client.post(
        "/api/batches",
        json={
            "asset_ids": ["S01", "S02"],
            "pipeline_stage_id": "motion-generation",
            "pipeline_values_by_asset": {"S01": {"prompt": "batch S01 override"}, "S02": {"prompt": "batch S02 override"}},
        },
    )

    assert response.status_code == 200, response.text
    with backend.db() as connection:
        rows = connection.execute("SELECT asset_id, payload FROM tasks WHERE project_id = ? ORDER BY sequence", ("project-a",)).fetchall()
    assert [row["asset_id"] for row in rows] == ["S01", "S02"]
    for row, expected in zip(rows, ("batch S01 override", "batch S02 override")):
        payload = json.loads(row["payload"])
        snapshot = json.loads(Path(payload["execution_snapshot"]["api_graph"]).read_text(encoding="utf-8"))
        assert snapshot["prompt"]["inputs"]["prompt"] == expected
        assert payload["project_id"] == "project-a"


def test_batch_without_overrides_keeps_each_segment_plan_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    segments = []
    for asset_id, prompt in (("S01", "plan prompt one"), ("S02", "plan prompt two")):
        source = tmp_path / f"{asset_id}.json"
        source.write_text(json.dumps({"prompt": {"class_type": "Video", "inputs": {"prompt": "source prompt"}}}), encoding="utf-8")
        segments.append({"id": asset_id, "workflow": str(source), "prompt": prompt})
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    stage = {
        "id": "motion-generation",
        "title": "视频",
        "backend": "ComfyUI 工作流",
        "execution": {"mode": "comfyui"},
        "input_specs": [{"id": "prompt", "label": "提示词", "kind": "文本", "required": True, "control": "text", "binding": {"node_id": "prompt", "input": "prompt"}}],
    }
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(backend, "PLAN_PATH", plan)
    monkeypatch.setattr(backend, "TASK_TEMPLATES", {})
    monkeypatch.setattr(backend, "PIPELINE_STAGES", [stage])

    response = client.post("/api/batches", json={"asset_ids": ["S01", "S02"], "pipeline_stage_id": "motion-generation"})

    assert response.status_code == 200, response.text
    with backend.db() as connection:
        rows = connection.execute("SELECT asset_id, payload FROM tasks WHERE project_id = ? ORDER BY sequence", ("project-a",)).fetchall()
    for row, expected in zip(rows, ("plan prompt one", "plan prompt two")):
        payload = json.loads(row["payload"])
        snapshot = json.loads(Path(payload["execution_snapshot"]["api_graph"]).read_text(encoding="utf-8"))
        assert snapshot["prompt"]["inputs"]["prompt"] == expected


def test_tasks_table_keeps_same_asset_id_separate_between_projects(client: TestClient) -> None:
    _insert_task("project-a", "S01")
    _insert_task("project-b", "S01")
    with sqlite3.connect(backend.DB_PATH) as connection:
        rows = connection.execute(
            "SELECT project_id, asset_id FROM tasks WHERE asset_id = 'S01' ORDER BY project_id"
        ).fetchall()
    assert rows == [("project-a", "S01"), ("project-b", "S01")]
