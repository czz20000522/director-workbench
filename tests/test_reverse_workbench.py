from __future__ import annotations

import json
import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from backend import app as backend


@pytest.fixture()
def reverse_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ComfyUI-Workspace" / "project-a"
    analysis_path = workspace / "reference-analysis" / "ref-test" / "analysis.json"
    analysis_path.parent.mkdir(parents=True)
    analysis_path.write_text(json.dumps({
        "id": "ref-test", "project_id": "project-a", "stages": [{"id": "production", "status": "waiting"}],
        "timeline": [{"id": "R01", "start_seconds": 0, "end_seconds": 5, "frame": "source/frame.jpg", "inference": "模型观察", "user_note": ""}],
        "artifacts": {"story": {"summary": "模型猜测", "provenance": "model_inference", "user_status": "unreviewed", "status": "pending_analysis", "evidence": ["R01"]}},
    }), encoding="utf-8")
    manifest = {"id": "project-a", "workspace_root": str(workspace), "plan_path": str(workspace / "plans" / "plan.json")}
    monkeypatch.setattr(backend, "ROOT", tmp_path)
    monkeypatch.setattr(backend, "PROJECT", tmp_path)
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "state.sqlite3")
    monkeypatch.setattr(backend, "load_project_manifest", lambda project_id: manifest)
    monkeypatch.setattr(backend, "CURRENT_PROJECT_ID", "project-a")
    scheduler = backend.task_scheduler.TaskScheduler(lambda: backend.db(), backend.DB_LOCK, backend.GPU_ADMISSION_LOCK,
        lambda: backend.scheduler_resource_reason(), lambda task: backend.revalidate_queued_task(task),
        lambda task: backend.execute_scheduled_task(task))
    monkeypatch.setattr(backend, 'TASK_SCHEDULER', scheduler)
    def fake_request(path, *args, **kwargs):
        assert path == '/queue', f'Unexpected model/network request: {path}'
        return {'queue_running': [], 'queue_pending': []}
    monkeypatch.setattr(backend, 'request_json', fake_request)
    return TestClient(backend.app), manifest, analysis_path


def seed_gpu_waiter(task_id='waiting-generation'):
    """An already-frozen GPU row in the isolated DB, without running any model."""
    with backend.db() as connection:
        connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
            (task_id, 'S01', 'scheduler_waiting', time.time(), time.time(), json.dumps({'asset_id': 'S01'}), 'project-a'))
        connection.commit()
    return task_id


def test_semantic_progress_requires_matching_checkpoint(reverse_client, monkeypatch):
    from tools import reference_semantic as semantic
    client, _, path = reverse_client
    monkeypatch.setattr(semantic, 'ROOT', backend.ROOT)
    document = json.loads(path.read_text(encoding='utf-8'))
    frames = semantic.pick_frames(document)
    signature = semantic.checkpoint_signature(document, frames)
    job = backend.background_jobs.create(backend.analysis_job_root(), 'project-a', 'ref-test')
    backend.background_jobs.update(backend.analysis_job_root(), job, status='running')
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs/current'
    assert 'progress' not in client.get(endpoint).json()['job']
    checkpoint = path.with_name('semantic-observations.json')
    semantic.save_observation_checkpoint(checkpoint, signature, [
        {'segment_id': 'R01', 'observation': 'visible'},
        {'segment_id': 'R01', 'observation': 'duplicate'},
        {'segment_id': 'foreign', 'observation': 'not this source'},
    ])
    result = client.get(endpoint).json()['job']
    assert result['status'] == 'running'
    assert result['progress'] == {'observed_frames': 1, 'total_frames': 1}
    semantic.save_observation_checkpoint(checkpoint, {**signature, 'analysis_id': 'other'}, [])
    assert 'progress' not in client.get(endpoint).json()['job']
    checkpoint.write_text('{incomplete', encoding='utf-8')
    assert client.get(endpoint).status_code == 200
    assert 'progress' not in client.get(endpoint).json()['job']


def test_classification_correction_retains_original_and_rejects_conflicts(reverse_client):
    client, manifest, path = reverse_client
    document = json.loads(path.read_text(encoding='utf-8'))
    document.update(content_mode='music_performance', content_mode_reason='模型猜测', revision=3)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    plan_path = Path(manifest['plan_path'])
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text('{"revision":18,"segments":[]}', encoding='utf-8')
    before_plan = plan_path.read_bytes()
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/classification'
    request = {'expected_revision': 3, 'content_mode': 'showcase', 'reason': '仅角色问候，无音乐证据'}
    response = client.patch(endpoint, json=request)
    assert response.status_code == 200, response.text
    corrected = response.json()['analysis']
    assert corrected['revision'] == 4
    assert corrected['content_mode'] == 'showcase'
    assert corrected['content_mode_user_status'] == 'reviewed'
    assert corrected['content_mode_original_candidate']['content_mode'] == 'music_performance'
    assert corrected['timeline'] == document['timeline']
    assert corrected['artifacts'] == document['artifacts']
    assert plan_path.read_bytes() == before_plan
    before_conflict = path.read_bytes()
    assert client.patch(endpoint, json=request).status_code == 409
    assert path.read_bytes() == before_conflict
    response = client.patch(endpoint, json={**request, 'expected_revision': 4, 'content_mode': 'other'})
    assert response.status_code == 200
    assert len(response.json()['analysis']['content_mode_history']) == 2
    assert response.json()['analysis']['content_mode_original_candidate'] == corrected['content_mode_original_candidate']


def test_classification_invalid_requests_do_not_write(reverse_client):
    client, _, path = reverse_client
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/classification'
    valid = {'expected_revision': 0, 'content_mode': 'showcase', 'reason': '角色展示'}
    before = path.read_bytes()
    for request in ({**valid, 'reason': '  '}, {**valid, 'content_mode': 'unknown'},
                    {**valid, 'expected_revision': True}, {**valid, 'unexpected': 1},
                    {'content_mode': 'showcase', 'reason': '角色展示'}):
        assert client.patch(endpoint, json=request).status_code == 422
        assert path.read_bytes() == before


def test_conversion_is_repeatable_and_preserves_director_edits(reverse_client) -> None:
    client, manifest, analysis_path = reverse_client
    endpoint = "/api/projects/project-a/reverse-analyses/ref-test"
    corrected = client.patch(endpoint + "/timeline/R01", json={"user_note": "本作改为回头微笑", "status": "reviewed"})
    assert corrected.status_code == 200
    first = client.post(endpoint + "/convert")
    assert first.status_code == 200, first.text
    segment = first.json()["created_segments"][0]
    assert segment["performance"] == "本作改为回头微笑"
    assert segment["source_revision"] == corrected.json()['analysis']['revision']
    assert segment["source_range_seconds"] == [0, 5]
    assert segment["source_note_status"] == 'reviewed'
    assert segment['source_observation'] == '模型观察'
    assert segment['source_user_note'] == '本作改为回头微笑'
    assert segment["reference_frame"] == "source/frame.jpg"
    assert segment["keyframes"]["first"] is None
    assert segment["workflow"] is None
    plan_path = Path(manifest["plan_path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["segments"][0]["performance"] = "导演已在制作页修改"
    plan["segments"][0]["workflow"] = "approved-workflow.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    revision = plan["revision"]
    second = client.post(endpoint + "/convert")
    assert second.status_code == 200
    assert second.json()["created_segments"] == []
    assert second.json()["plan"]["segments"][0]["performance"] == "导演已在制作页修改"
    assert second.json()["plan"]["segments"][0]["workflow"] == "approved-workflow.json"
    assert second.json()["plan"]["revision"] == revision
    assert second.json()["plan"]["segments"][0]["source_revision"] == segment['source_revision']
    assert second.json()["plan"]["segments"][0]["source_range_seconds"] == [0, 5]
    seed = json.loads(analysis_path.with_name("production-seed.json").read_text(encoding="utf-8"))
    assert seed["created_segments"] == [segment["id"]]


def test_reconversion_preserves_nested_merges_of_reference_samples(reverse_client):
    client, _, path = reverse_client
    document = json.loads(path.read_text(encoding='utf-8'))
    document['timeline'] = [
        {**document['timeline'][0], 'id': f'R{index:02d}', 'start_seconds': index - 1, 'end_seconds': index}
        for index in range(1, 4)
    ]
    path.write_text(json.dumps(document), encoding='utf-8')
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test'
    converted = client.post(endpoint + '/convert').json()
    ids = [item['id'] for item in converted['created_segments']]
    plan = converted['plan']
    # Merge the tail first, then merge that combined shot into the head.
    for first, second in [(ids[1], ids[2]), (ids[0], ids[1])]:
        result = client.post(f'/api/projects/project-a/plan/segments/{first}/operate', json={
            'operation': 'merge', 'target_id': second, 'expected_revision': plan['revision'],
        })
        assert result.status_code == 200, result.text
        plan = result.json()['plan']
    assert len(plan['segments']) == 1
    assert plan['segments'][0]['duration_seconds'] == 3
    revision = client.get(endpoint).json()['revision']
    repeated = client.post(endpoint + '/convert', json={'expected_revision': revision})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['created_segments'] == []
    assert repeated.json()['plan'] == plan


def test_series_adaptation_changes_require_review_again(reverse_client) -> None:
    client, _, _ = reverse_client
    first = client.post("/api/project-state/creative", json={"values": {"creation_mode": "reference-series", "reference_series": "参考系列", "adaptation": "保留节奏"}})
    assert first.status_code == 200
    converted = client.post("/api/projects/project-a/reverse-analyses/ref-test/convert").json()
    segment_id = converted["created_segments"][0]["id"]
    review = client.post("/api/project-state/reviews", json={"asset_id": segment_id, "stage": "sample", "status": "approved"})
    assert review.status_code == 200
    changed = client.post("/api/project-state/creative", json={"values": {"creation_mode": "reference-series", "reference_series": "参考系列", "adaptation": "改为完全不同的节奏"}})
    assert changed.status_code == 200
    state = client.get("/api/project-state").json()
    assert state["creative"]["values"]["reference_series"] == "参考系列"
    assert state["reviews"][segment_id]["sample"]["status"] == "stale"


def test_human_correction_retains_original_evidence(reverse_client) -> None:
    client, _, _ = reverse_client
    endpoint = "/api/projects/project-a/reverse-analyses/ref-test/artifacts/story"
    first = client.patch(endpoint, json={"summary": "人工修订", "user_status": "reviewed"})
    assert first.status_code == 200
    artifact = first.json()["artifact"]
    assert artifact["provenance"] == "user_confirmed"
    assert artifact["status"] == "ready"
    assert artifact["original_candidate"]["summary"] == "模型猜测"
    assert artifact["evidence"] == ["R01"]
    second = client.patch(endpoint, json={"summary": "第二次人工修订", "user_status": "reviewed"})
    assert second.json()["artifact"]["original_candidate"]["summary"] == "模型猜测"


def test_new_analysis_only_appends_its_own_drafts(reverse_client) -> None:
    client, manifest, analysis_path = reverse_client
    client.post("/api/projects/project-a/reverse-analyses/ref-test/convert")
    document = json.loads(analysis_path.read_text(encoding="utf-8"))
    document["id"] = "ref-second"
    second_path = analysis_path.parent.parent / "ref-second" / "analysis.json"
    second_path.parent.mkdir()
    second_path.write_text(json.dumps(document), encoding="utf-8")
    response = client.post("/api/projects/project-a/reverse-analyses/ref-second/convert")
    assert response.status_code == 200
    segments = response.json()["plan"]["segments"]
    assert len(segments) == 2
    assert [segment["source_analysis"] for segment in segments] == ["ref-test", "ref-second"]
    assert segments[1]["start_seconds"] == 5


def prepare_semantic(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"test")
    monkeypatch.setattr(backend, "PRIMARY_MODEL", model)
    monkeypatch.setattr(backend, "PRIMARY_MMPROJ", model)
    monkeypatch.setattr(backend, "comfy_status", lambda: {"online": False})


def test_background_analysis_returns_receipt_deduplicates_and_survives_client_reload(reverse_client, monkeypatch, tmp_path):
    client, _, analysis_path = reverse_client
    prepare_semantic(monkeypatch, tmp_path)
    running = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_update = backend.background_jobs.update

    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        if kwargs.get('status') in {'succeeded', 'failed'}: finished.set()
        return result

    def command(args, **kwargs):
        running.set()
        assert release.wait(3)
        result_path = Path(args[args.index('--result-output') + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({'artifacts': {'story': {'summary': '后台结果', 'evidence': ['R01']}}}), encoding='utf-8')

    monkeypatch.setattr(backend.background_jobs, 'update', update)
    monkeypatch.setattr(backend, 'run_reference_command', command)
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs'
    try:
        response = client.post(endpoint)
        assert response.status_code == 202, response.text
        job_id = response.json()['job']['id']
        assert response.json()['job']['status'] == 'queued'
        assert backend.get_task(job_id)['status'] == 'scheduler_waiting'
        assert not running.is_set(), 'admission must only persist, not start the model'
        assert backend.TASK_SCHEDULER.tick() == [job_id]
        assert running.wait(1)
        assert client.post(endpoint).json()['job']['id'] == job_id
        reloaded = TestClient(backend.app).get(endpoint + '/current').json()['job']
        assert reloaded['id'] == job_id and reloaded['status'] == 'running'
        assert 'owner' not in reloaded
        other_id = seed_gpu_waiter()
        assert backend.TASK_SCHEDULER.tick(threaded=False) == []
        assert backend.get_task(other_id)['status'] == 'scheduler_waiting'
        assert backend.TASK_SCHEDULER.queue_info(other_id)['reason']['code'] == 'waiting_execution_slot'
    finally:
        release.set()
        assert finished.wait(3)
    assert client.get(endpoint + '/current').json()['job']['status'] == 'succeeded'
    assert json.loads(analysis_path.read_text(encoding='utf-8'))['artifacts']['story']['summary'] == '后台结果'
    assert not backend.REVERSE_SEMANTIC_LOCK.locked()


def test_background_analysis_failure_is_persisted(reverse_client, monkeypatch, tmp_path):
    client, _, path = reverse_client
    prepare_semantic(monkeypatch, tmp_path)
    before = path.read_bytes()
    finished = threading.Event()
    original_update = backend.background_jobs.update
    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        if kwargs.get('status') == 'failed': finished.set()
        return result
    def fail(*args, **kwargs): raise HTTPException(504, '受控分析超时')
    monkeypatch.setattr(backend.background_jobs, 'update', update)
    monkeypatch.setattr(backend, 'run_reference_command', fail)
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs'
    response = client.post(endpoint)
    assert response.status_code == 202
    assert backend.TASK_SCHEDULER.tick(threaded=False) == [response.json()['job']['id']]
    assert finished.wait(3)
    job = client.get(endpoint + '/current').json()['job']
    assert job['status'] == 'failed' and job['error'] == '受控分析超时'
    assert path.read_bytes() == before
    assert not backend.REVERSE_SEMANTIC_LOCK.locked()


def test_server_restart_marks_analysis_uncertain_and_holds_queued_gpu_work(reverse_client, monkeypatch):
    client, _, _ = reverse_client
    job = backend.background_jobs.create(backend.analysis_job_root(), 'project-a', 'ref-test')
    backend.background_jobs.update(backend.analysis_job_root(), job, owner='previous-server', status='running')
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs'
    assert client.get(endpoint + '/current').json()['job']['status'] == 'needs_reconcile'
    prepare_semantic(monkeypatch, backend.ROOT)
    assert client.post(endpoint).json()['job']['id'] == job['id']
    other_id = seed_gpu_waiter()
    assert backend.TASK_SCHEDULER.tick(threaded=False) == []
    assert backend.get_task(other_id)['status'] == 'scheduler_waiting'
    assert backend.TASK_SCHEDULER.queue_info(other_id)['reason']['code'] == 'analysis_needs_reconcile'


def test_semantic_result_merges_edits_saved_during_analysis(reverse_client, monkeypatch, tmp_path) -> None:
    client, _, analysis_path = reverse_client
    prepare_semantic(monkeypatch, tmp_path)

    def complete_after_edit(command, **kwargs):
        frozen_input = Path(command[command.index('--analysis') + 1])
        assert frozen_input != analysis_path
        assert json.loads(frozen_input.read_text(encoding='utf-8'))['artifacts']['story']['summary'] == '模型猜测'
        backend.update_reverse_artifact("project-a", "ref-test", "story", backend.ReverseArtifactUpdateRequest(summary="另一设备刚保存的校订"))
        backend.update_reverse_timeline("project-a", "ref-test", "R01", backend.ReverseTimelineUpdateRequest(user_note="另一设备确认的表演"))
        result_path = Path(command[command.index("--result-output") + 1])
        result_path.parent.mkdir(parents=True)
        result_path.write_text(json.dumps({"timeline": [{"segment_id": "R01", "observation": "新观察"}], "artifacts": {"story": {"summary": "新模型候选", "confidence": 0.5}}}), encoding="utf-8")

    monkeypatch.setattr(backend, "run_reference_command", complete_after_edit)
    response = client.post("/api/projects/project-a/reverse-analyses/ref-test/semantic")
    assert response.status_code == 202, response.text
    job_id = response.json()['job']['id']
    assert client.post('/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs').json()['job']['id'] == job_id
    assert backend.TASK_SCHEDULER.tick(threaded=False) == [job_id]
    assert client.get('/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs/current').json()['job']['status'] == 'succeeded'
    document = json.loads(analysis_path.read_text(encoding='utf-8'))
    assert document["artifacts"]["story"]["summary"] == "另一设备刚保存的校订"
    assert document["artifacts"]["story"]["model_candidate"]["summary"] == "新模型候选"
    assert document["timeline"][0]["user_note"] == "另一设备确认的表演"
    assert document["timeline"][0]["status"] == "reviewed"
    assert json.loads(analysis_path.read_text(encoding="utf-8")) == document
    assert not backend.REVERSE_SEMANTIC_LOCK.locked()


def test_semantic_failure_preserves_archive_and_releases_reservation(reverse_client, monkeypatch, tmp_path) -> None:
    client, _, analysis_path = reverse_client
    prepare_semantic(monkeypatch, tmp_path)
    before = analysis_path.read_bytes()

    def fail(*args, **kwargs):
        raise HTTPException(504, "分析超时")

    monkeypatch.setattr(backend, "run_reference_command", fail)
    response = client.post("/api/projects/project-a/reverse-analyses/ref-test/semantic")
    assert response.status_code == 202
    task_id = response.json()['job']['id']
    assert backend.TASK_SCHEDULER.tick(threaded=False) == [task_id]
    assert backend.get_task(task_id)['status'] == 'failed'
    assert backend.get_task(task_id)['error'] == '分析超时'
    assert analysis_path.read_bytes() == before
    assert not backend.REVERSE_SEMANTIC_LOCK.locked()


def test_running_semantic_holds_accepted_gpu_work_until_boundary(reverse_client, monkeypatch) -> None:
    client, _, _ = reverse_client
    task_id = seed_gpu_waiter()
    executed = []
    def fake_execute(task):
        executed.append(task['id'])
        backend.set_task(task['id'], status='succeeded')
    monkeypatch.setattr(backend, 'execute_scheduled_task', fake_execute)
    with backend.REVERSE_SEMANTIC_LOCK:
        assert backend.TASK_SCHEDULER.tick(threaded=False) == []
        assert backend.get_task(task_id)['status'] == 'scheduler_waiting'
        assert backend.TASK_SCHEDULER.queue_info(task_id)['reason']['code'] == 'reference_running'
        assert executed == []
    assert backend.TASK_SCHEDULER.tick(threaded=False) == [task_id]
    assert executed == [task_id]


def test_external_comfy_queue_holds_semantic_after_admission(reverse_client, monkeypatch, tmp_path) -> None:
    client, _, _ = reverse_client
    prepare_semantic(monkeypatch, tmp_path)
    queue = {'queue_running': [['external-task']], 'queue_pending': []}
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: queue)
    executions = []
    monkeypatch.setattr(backend, 'execute_scheduled_task', lambda task: (executions.append(task['id']), backend.set_task(task['id'], status='succeeded')))
    response = client.post("/api/projects/project-a/reverse-analyses/ref-test/semantic")
    assert response.status_code == 202
    job_id = response.json()['job']['id']
    assert backend.TASK_SCHEDULER.tick(threaded=False) == []
    assert executions == []
    assert backend.get_task(job_id)['status'] == 'scheduler_waiting'
    assert backend.TASK_SCHEDULER.queue_info(job_id)['reason']['code'] == 'external_queue_busy'
    queue['queue_running'] = []
    assert backend.TASK_SCHEDULER.tick(threaded=False) == [job_id]
    assert executions == [job_id]
    assert not backend.REVERSE_SEMANTIC_LOCK.locked()


def test_reference_timeout_stops_owned_process_tree(monkeypatch) -> None:
    class Process:
        pid = 12345
        killed = False

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("test-command", timeout)
            return "", ""

        def poll(self):
            return None if not self.killed else -1

        def kill(self):
            self.killed = True

    process = Process()
    calls = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(backend.subprocess, "run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(HTTPException) as raised:
        backend.run_reference_command(["test-command"], timeout=1, label="参考分析")
    assert raised.value.status_code == 504
    assert process.killed
    if backend.os.name == "nt":
        assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]


@pytest.mark.parametrize('queue', [None, {'queue_running': [['external']], 'queue_pending': []}])
def test_resume_persists_new_attempt_until_resources_confirmed(reverse_client, monkeypatch, queue) -> None:
    client, _, _ = reverse_client
    def fake_request(*args, **kwargs):
        if queue is None:
            raise ConnectionError('Fake ComfyUI offline')
        return queue
    monkeypatch.setattr(backend, 'request_json', fake_request)
    executions = []
    monkeypatch.setattr(backend, 'execute_scheduled_task', lambda task: executions.append(task['id']))
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)",
            ("stopped-attempt", "S01", "stopped", time.time(), time.time(), json.dumps({"asset_id": "S01"}), "project-a"),
        )
        connection.commit()
    response = client.post("/api/tasks/stopped-attempt/resume")
    assert response.status_code == 200, response.text
    attempt = response.json()
    assert attempt['id'] != 'stopped-attempt'
    assert attempt['status'] == 'scheduler_waiting'
    assert backend.TASK_SCHEDULER.tick(threaded=False) == []
    assert executions == []
    assert backend.TASK_SCHEDULER.queue_info(attempt['id'])['reason']['code'] == ('resource_status_unknown' if queue is None else 'external_queue_busy')
    with backend.db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert backend.get_task('stopped-attempt')['status'] == 'stopped'


def test_status_stream_does_not_block_other_event_loop_work(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_status():
        started.set()
        release.wait(timeout=2)
        return {"comfy": {"online": False}}

    monkeypatch.setattr(backend, "status", slow_status)
    monkeypatch.setattr(backend, "tasks", lambda: [])

    async def check():
        response = await backend.events()
        first = asyncio.create_task(anext(response.body_iterator))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # While status is waiting, other requests must get an event-loop turn.
            assert not first.done()
        finally:
            release.set()
        message = await asyncio.wait_for(first, 1)
        assert '"online": false' in message
        await response.body_iterator.aclose()

    asyncio.run(check())
