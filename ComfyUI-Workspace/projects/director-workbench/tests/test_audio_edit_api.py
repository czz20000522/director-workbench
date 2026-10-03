import json
import wave
from pathlib import Path

import pytest

from backend import app as backend
from test_production_presets import preset_client

ENDPOINT = '/api/projects/preset-test/audio-edit-tasks'
REAL_CAPACITY = backend.require_submission_capacity


def register_audio(project, asset_id='voice'):
    path = project / f'{asset_id}.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 16000)
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': asset_id, 'kind': 'audio', 'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)
    return path


def submit(client, **extra):
    response = client.post(ENDPOINT, json={'source_asset_id': 'voice', 'idempotency_key': 'edit-one', **extra})
    assert response.status_code == 202, response.text
    return response.json()


def claim(task):
    """Represent a fake scheduler dispatch before testing the CPU receipt path."""
    assert backend.get_task(task['id'])['status'] == 'scheduler_waiting'
    backend.set_task(task['id'], status='preparing')


def test_edit_source_scope_parameters_and_no_comfy(preset_client, monkeypatch):
    client, project = preset_client
    source = register_audio(project)
    foreign = backend.blank_project_manifest('foreign', 'test', 'foreign', project / 'foreign')
    foreign['assets'] = [{'id': 'foreign-project-voice', 'kind': 'audio', 'sources': {'A': str(source)}}]
    backend.save_project_manifest(foreign)
    monkeypatch.setattr(backend, 'comfy_status', lambda: pytest.fail('CPU edit queried Comfy'))
    monkeypatch.setattr(backend, 'request_json', lambda *a, **k: pytest.fail('CPU edit contacted Comfy'))
    for extra in ({'source_asset_id': str(source)}, {'source_asset_id': 'foreign-project-voice'},
                  {'background_asset_id': 'foreign'}, {'gain': True}, {'gain': 9},
                  {'source': str(source)}, {'duration_seconds': 0}):
        response = client.post(ENDPOINT, json={'source_asset_id': 'voice', 'idempotency_key': 'bad', **extra})
        assert response.status_code == 422, response.text
    task = submit(client)
    assert task['payload']['kind'] == 'audio_edit'
    assert task['payload']['execution_owner']['pid'] > 0
    assert task['payload']['source_asset']['asset_id'] == 'voice'
    assert Path(task['payload']['request']['source']).read_bytes() == source.read_bytes()


def test_edit_idempotency_precedes_admission_and_source_lookup(preset_client):
    client, project = preset_client
    source = register_audio(project)
    first = submit(client)
    source.write_bytes(b'changed source')
    assert submit(client)['id'] == first['id']
    assert client.post(ENDPOINT, json={'source_asset_id': 'voice', 'idempotency_key': 'edit-one', 'gain': .5}).status_code == 409
    # A different key is admitted independently, but this changed source is no
    # longer valid WAV input and must fail validation rather than global busy.
    assert client.post(ENDPOINT, json={'source_asset_id': 'voice', 'idempotency_key': 'edit-two'}).status_code == 422
    assert client.get('/api/projects/preset-test/submission-receipt', params={'key': 'edit-one'}).json()['id'] == first['id']


def test_edit_frozen_input_independent_candidate_and_recovery(preset_client, monkeypatch):
    client, project = preset_client
    source = register_audio(project)
    original_assets = backend.require_project_manifest('preset-test')['assets']
    task = submit(client, start_seconds=.25, duration_seconds=1, gain=.5)
    claim(task)
    source.write_bytes(b'original subsequently changed')
    receipt = backend.audio_edit_tasks.execute(task['payload'])
    backend.mark_orphans()
    monkeypatch.setattr(backend, 'ensure_no_unresolved_analysis_jobs', lambda: pytest.fail('CPU recovery entered GPU admission'))
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    recovered = client.post(detail + '/reconcile')
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'succeeded'
    assert recovered.json()['result']['frames'] == 8000
    assets = backend.require_project_manifest('preset-test')['assets']
    assert assets[:-1] == original_assets
    assert assets[-1]['id'] == f"audio-edit-{task['id']}"
    assert assets[-1]['sources']['A'] != str(source)
    assert assets[-1]['edit_parameters']['gain'] == .5
    assert source.read_bytes() == b'original subsequently changed'
    backend.complete_audio_edit_task(recovered.json(), receipt)
    assert backend.require_project_manifest('preset-test')['assets'] == assets
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


@pytest.mark.parametrize('late', [False, True])
def test_cpu_stop_does_not_claim_running_executor_killed_or_adopt(preset_client, monkeypatch, late):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    real_execute = backend.audio_edit_tasks.execute
    def execute(payload, cancelled):
        if late:
            result = real_execute(payload, cancelled)
            assert client.post(detail + '/stop').json()['status'] == 'stop_requested'
            return result
        return real_execute(payload, cancelled)
    monkeypatch.setattr(backend.audio_edit_tasks, 'execute', execute)
    if not late:
        assert client.post(detail + '/stop').json()['status'] == 'stopped'
        monkeypatch.setattr(backend.audio_edit_tasks, 'execute', lambda *args: pytest.fail('Cancelled pending CPU work executed'))
    else:
        claim(task)
        backend.run_audio_edit_task(task['id'], task['payload'])
    assert client.get(detail).json()['status'] == 'stopped'
    assert client.post(detail + '/resume').json()['status'] == 'stopped'
    assert len(backend.require_project_manifest('preset-test')['assets']) == 1
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_missing_cpu_receipt_requires_ended_owner_then_releases_cpu_slot(preset_client, monkeypatch):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    claim(task)
    backend.mark_orphans()
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    monkeypatch.setattr(backend, 'request_json', lambda *a, **k: pytest.fail('CPU recovery contacted Comfy'))
    assert client.post(detail + '/reconcile').status_code == 409
    assert client.post(detail + '/resume').status_code == 409
    args = {'confirm_execution_ended': True, 'note': '确认旧进程结束并检查输出'}
    assert client.post(detail + '/resolve-missing', json=args).status_code == 409
    monkeypatch.setattr(backend, 'audio_edit_owner_alive', lambda payload: False)
    done = client.post(detail + '/resolve-missing', json=args)
    assert done.status_code == 200, done.text
    assert done.json()['status'] == 'failed'
    assert done.json()['result']['missing_execution_resolution']['kind'] == 'audio_edit'
    assert submit(client)['id'] == task['id']
    assert submit(client, idempotency_key='new-confirmed-operation')['id'] != task['id']


def test_cpu_does_not_block_gpu_capacity_or_release_but_protects_project(preset_client, monkeypatch):
    client, project = preset_client
    register_audio(project)
    submit(client)
    backend.ensure_no_release_blocking_tasks('GPU', gpu_only=True)
    with pytest.raises(backend.HTTPException):
        backend.ensure_no_release_blocking_tasks('修改项目')
    seen = []
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': True, 'queue_running': 0, 'queue_pending': 0, 'gpu_free_mib': 1024, 'ram_free_gib': 8})
    assert REAL_CAPACITY()['accepted_for_queue']
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: seen.append(path) or ({'queue_running': [], 'queue_pending': []} if path == '/queue' else {}))
    assert client.post('/api/resources/release').status_code == 200
    assert seen == ['/queue', '/free']


def test_valid_cpu_receipt_cannot_be_discarded_as_missing(preset_client, monkeypatch):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    claim(task)
    backend.audio_edit_tasks.execute(task['payload'])
    backend.mark_orphans()
    monkeypatch.setattr(backend, 'audio_edit_owner_alive', lambda payload: False)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    assert client.post(detail + '/resolve-missing', json={'confirm_execution_ended': True, 'note': 'checked'}).status_code == 409
    assert client.post(detail + '/reconcile').json()['status'] == 'succeeded'


def test_cpu_background_execution_while_gpu_task_active(preset_client):
    client, project = preset_client
    source = register_audio(project)
    background = register_audio(project, 'music')
    original = (source.read_bytes(), background.read_bytes())
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute("INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES ('gpu','S01','queued',1,1,'{}','preset-test')")
        connection.commit()
    task = submit(client, background_asset_id='music', duration_seconds=1, gain=.5,
                  fade_in_seconds=.1, fade_out_seconds=.2, background_offset_seconds=.25)
    backend.run_audio_edit_task(task['id'], task['payload'])
    result = backend.get_task(task['id'])
    assert result['status'] == 'succeeded'
    assert result['result']['report']['background_mixed_frames'] == 6000
    assert result['payload']['execution_finished_at'] > 0
    assert (source.read_bytes(), background.read_bytes()) == original
    assert backend.get_task('gpu')['status'] == 'queued'


def test_cpu_owner_identity_is_conservative_and_handles_pid_reuse(monkeypatch):
    with pytest.raises(backend.HTTPException):
        backend.audio_edit_owner_alive({})
    class Process:
        def create_time(self): return 200
    monkeypatch.setattr(backend.psutil, 'Process', lambda pid: Process())
    assert backend.audio_edit_owner_alive({'execution_owner': {'pid': 12, 'created_at': 200}})
    assert not backend.audio_edit_owner_alive({'execution_owner': {'pid': 12, 'created_at': 100}})
    def denied(pid): raise backend.psutil.AccessDenied(pid)
    monkeypatch.setattr(backend.psutil, 'Process', denied)
    with pytest.raises(backend.HTTPException):
        backend.audio_edit_owner_alive({'execution_owner': {'pid': 12, 'created_at': 200}})


def test_cpu_execution_error_can_be_confirmed_without_service_restart(preset_client, monkeypatch):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    def fail(*args): raise OSError('disk error')
    monkeypatch.setattr(backend.audio_edit_tasks, 'execute', fail)
    backend.run_audio_edit_task(task['id'], task['payload'])
    current = backend.get_task(task['id'])
    assert current['status'] == 'needs_reconcile'
    assert not backend.audio_edit_owner_alive(current['payload'])
    response = client.post(f"/api/projects/preset-test/tasks/{task['id']}/resolve-missing",
        json={'confirm_execution_ended': True, 'note': '同步执行已返回；检查无可恢复输出'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'failed'


def test_waiting_cpu_restart_keeps_same_unexecuted_receipt(preset_client):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    backend.mark_orphans()
    detail = client.get(f"/api/projects/preset-test/tasks/{task['id']}").json()
    assert detail['status'] == 'scheduler_waiting'
    assert detail['result'] is None
    assert submit(client)['id'] == task['id']
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_claimed_cpu_stop_waits_for_confirmed_cancel_before_adoption(preset_client, monkeypatch):
    client, project = preset_client
    register_audio(project)
    task = submit(client)
    claim(task)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    assert client.post(detail + '/stop').json()['status'] == 'stop_requested'
    called = []
    def cancelled_executor(payload, cancelled):
        called.append(payload)
        assert cancelled()
        raise backend.audio_edit_tasks.AudioEditStoppedBeforeStart('Confirmed CPU cancellation')
    monkeypatch.setattr(backend.audio_edit_tasks, 'execute', cancelled_executor)
    backend.run_audio_edit_task(task['id'], task['payload'])
    assert len(called) == 1
    assert client.get(detail).json()['status'] == 'stopped'
    assert len(backend.require_project_manifest('preset-test')['assets']) == 1
