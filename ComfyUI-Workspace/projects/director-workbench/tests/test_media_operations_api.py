"""HTTP contract/recovery tests; executor subprocesses are replaced with recorded receipts."""
import json
from pathlib import Path

import pytest

from backend import app as backend
from test_production_presets import preset_client

ENDPOINT = '/api/projects/preset-test/media-tasks'
REAL_CAPACITY = backend.require_submission_capacity


@pytest.fixture(autouse=True)
def available(monkeypatch):
    monkeypatch.setattr(backend.media_operations, 'capability', lambda: {'operations': {
        name: {'available': True, 'missing': []} for name in ('silence', 'video_qc', 'transcribe', 'silent_video')}})


def source(project, kind='video', asset_id='source'):
    path = project / (asset_id + ('.mp4' if kind == 'video' else '.wav'))
    path.write_bytes(b'original registered media')
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': asset_id, 'kind': kind, 'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)
    return path


def submit(client, **extra):
    response = client.post(ENDPOINT, json={'operation': 'silence', 'duration_seconds': 1,
                                         'idempotency_key': 'media-one', **extra})
    assert response.status_code == 202, response.text
    return response.json()


def claim(task):
    """Represent a fake scheduler dispatch before testing execution receipts."""
    assert backend.get_task(task['id'])['status'] == 'scheduler_waiting'
    backend.set_task(task['id'], status='preparing')


def receipt(task):
    output = Path(task['payload']['output_dir'])
    media = output / 'candidate.wav'
    media.write_bytes(b'media candidate')
    report = output / 'report.json'
    report.write_text('{"duration_seconds":1}', encoding='utf-8')
    return {'task_id': task['id'], 'operation': task['payload']['operation'],
            'outputs': [{'kind': 'audio', 'path': str(media), 'title': '静音候选'},
                        {'kind': 'report', 'path': str(report), 'title': '检查报告'}],
            'report': {'duration_seconds': 1, 'approval': False}}


@pytest.mark.parametrize('body', [
    {'operation': 'silence'},
    {'operation': 'silence', 'duration_seconds': 0},
    {'operation': 'silence', 'duration_seconds': True},
    {'operation': 'silence', 'duration_seconds': 1, 'channels': True},
    {'operation': 'silence', 'duration_seconds': 1, 'source_asset_id': 'source'},
    {'operation': 'silence', 'duration_seconds': 1, 'sample_count': 12},
    {'operation': 'video_qc', 'source_asset_id': 'source', 'duration_seconds': None},
    {'operation': 'video_qc', 'source_asset_id': 'source', 'sample_count': 2},
    {'operation': 'transcribe', 'source_asset_id': 'source', 'channels': 1},
    {'operation': 'transcribe', 'source_asset_id': 'source', 'word_timestamps': 'true'},
    {'operation': 'silent_video', 'source_asset_id': 'source', 'expected_text': ''},
    {'operation': 'silent_video', 'source': 'arbitrary.mp4'},
])
def test_operation_parameters_are_exclusive(preset_client, body):
    client, _ = preset_client
    response = client.post(ENDPOINT, json={'idempotency_key': 'invalid', **body})
    assert response.status_code == 422, response.text


def test_asset_scope_frozen_source_and_idempotency_before_admission(preset_client, monkeypatch):
    client, project = preset_client
    original = source(project)
    for asset_id in (str(original), 'another-project-source'):
        response = client.post(ENDPOINT, json={'operation': 'video_qc', 'source_asset_id': asset_id, 'idempotency_key': 'invalid'})
        assert response.status_code == 422, response.text
    request = {'operation': 'video_qc', 'source_asset_id': 'source', 'idempotency_key': 'original-key', 'sample_count': 3}
    first = client.post(ENDPOINT, json=request)
    assert first.status_code == 202, first.text
    task = first.json()
    frozen = Path(task['payload']['request']['source']['path'])
    assert frozen.read_bytes() == original.read_bytes()
    original.write_bytes(b'changed source')
    assert frozen.read_bytes() == b'original registered media'
    monkeypatch.setattr(backend.media_operations, 'capability', lambda: pytest.fail('idempotent replay consulted capabilities'))
    assert client.post(ENDPOINT, json=request).json()['id'] == task['id']
    assert client.post(ENDPOINT, json={**request, 'sample_count': 4}).status_code == 409
    monkeypatch.setattr(backend.media_operations, 'capability', lambda: {'operations': {'video_qc': {'available': True}}})
    independent = client.post(ENDPOINT, json={**request, 'idempotency_key': 'new-key'})
    assert independent.status_code == 202, independent.text
    assert independent.json()['id'] != task['id']
    assert independent.json()['status'] == 'scheduler_waiting'
    assert client.get('/api/projects/preset-test/submission-receipt', params={'key': 'original-key'}).json()['id'] == task['id']


def test_recovery_registers_candidate_and_report_once_without_execution(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    claim(task)
    result = receipt(task)
    monkeypatch.setattr(backend.media_operations, 'read_receipt', lambda payload: result)
    monkeypatch.setattr(backend.media_operations, 'execute', lambda *args: pytest.fail('recovery reran media processing'))
    monkeypatch.setattr(backend, 'ensure_no_unresolved_analysis_jobs', lambda: pytest.fail('CPU recovery used GPU admission'))
    backend.mark_orphans()
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    response = client.post(detail + '/resume')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'succeeded'
    assert response.json()['result']['report']['approval'] is False
    outputs = response.json()['result']['outputs']
    assert outputs[0]['asset_id'] == f"media-{task['id']}"
    assert 'asset_id' not in outputs[1]
    assets = backend.require_project_manifest('preset-test')['assets']
    assert len(assets) == 1 and assets[0]['kind'] == 'audio'
    assert assets[0]['media_task_id'] == task['id']
    assert client.post(detail + '/reconcile').json()['status'] == 'succeeded'
    assert backend.require_project_manifest('preset-test')['assets'] == assets
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


@pytest.mark.parametrize('late', [False, True])
def test_stop_prevents_registration_and_resume_does_not_rerun(preset_client, monkeypatch, late):
    client, _ = preset_client
    task = submit(client)
    result = receipt(task)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    calls = []
    def execute(payload, cancelled):
        calls.append(payload)
        if late:
            assert client.post(detail + '/stop').json()['status'] == 'stop_requested'
            return result
        assert cancelled()
        raise backend.media_operations.MediaOperationStopped('stopped')
    monkeypatch.setattr(backend.media_operations, 'execute', execute)
    if not late:
        assert client.post(detail + '/stop').json()['status'] == 'stopped'
    else:
        claim(task)
        backend.run_media_operation_task(task['id'], task['payload'])
    assert client.get(detail).json()['status'] == 'stopped'
    assert client.post(detail + '/resume').json()['status'] == 'stopped'
    assert len(calls) == (1 if late else 0)
    assert backend.require_project_manifest('preset-test')['assets'] == []


def test_unknown_worker_owner_cannot_be_resolved_as_missing(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    claim(task)
    backend.mark_orphans()
    monkeypatch.setattr(backend, 'audio_edit_owner_alive', lambda payload: False)
    def unknown(payload):
        raise RuntimeError('worker identity unavailable')
    monkeypatch.setattr(backend.media_operations, 'owner_alive', unknown)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    response = client.post(detail + '/resolve-missing', json={'confirm_execution_ended': True, 'note': 'operator checked'})
    assert response.status_code == 409, response.text
    assert client.get(detail).json()['status'] == 'needs_reconcile'
    assert client.post(detail + '/resume').status_code == 409
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_media_cpu_submission_and_recovery_do_not_consult_comfy(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'comfy_status', lambda: pytest.fail('CPU submission queried GPU'))
    monkeypatch.setattr(backend, 'request_json', lambda *a, **k: pytest.fail('CPU contacted Comfy'))
    task = submit(client)
    backend.ensure_no_release_blocking_tasks('GPU', gpu_only=True)
    with pytest.raises(backend.HTTPException):
        backend.ensure_no_release_blocking_tasks('修改项目')
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': True, 'queue_running': 0, 'queue_pending': 0, 'gpu_free_mib': 1024, 'ram_free_gib': 8})
    assert REAL_CAPACITY()['accepted_for_queue']
    result = receipt(task)
    monkeypatch.setattr(backend.media_operations, 'execute', lambda *args: result)
    backend.run_media_operation_task(task['id'], task['payload'])
    assert backend.get_task(task['id'])['status'] == 'succeeded'


def test_primary_and_secondary_media_candidates_can_be_explicitly_adopted(preset_client):
    client, _ = preset_client
    task = submit(client)
    result = receipt(task)
    result['status'] = 'success'
    image = Path(task['payload']['output_dir']) / 'frame.png'
    image.write_bytes(b'test still')
    result['outputs'].append({'kind': 'image', 'path': str(image), 'title': '抽帧候选'})
    completed = backend.complete_media_operation_task(task, result)
    document, _ = backend.load_project_plan('preset-test')
    for output in completed['result']['outputs']:
        if output['kind'] == 'report':
            continue
        adopted = client.post('/api/projects/preset-test/adoptions', json={
            'asset_id': output['asset_id'], 'stage': 'sample', 'candidate_ref': output['path'],
            'expected_review_revision': 0, 'expected_plan_revision': document.get('revision', 0),
            'note': '人工检查通过', 'confirm_adoption': True,
        })
        assert adopted.status_code == 200, adopted.text
    assert completed['result']['outputs'][0]['asset_id'] != completed['result']['outputs'][2]['asset_id']


def test_invalid_snapshot_is_client_error_not_internal_server_error(preset_client, monkeypatch):
    client, _ = preset_client
    def invalid(*args, **kwargs):
        raise backend.media_operations.MediaOperationFailed('Linked media paths are not permitted')
    monkeypatch.setattr(backend.media_operations, 'prepare', invalid)
    response = client.post(ENDPOINT, json={'operation': 'silence', 'duration_seconds': 1, 'idempotency_key': 'bad-snapshot'})
    assert response.status_code == 422, response.text
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_waiting_media_restart_retains_request_without_replay(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    monkeypatch.setattr(backend.media_operations, 'execute', lambda *args: pytest.fail('Pending restart ran a media executor'))
    backend.mark_orphans()
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    assert client.get(detail).json()['status'] == 'scheduler_waiting'
    assert submit(client)['id'] == task['id']
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_claimed_media_stop_waits_for_executor_confirmation(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    claim(task)
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    assert client.post(detail + '/stop').json()['status'] == 'stop_requested'
    def cancelled_executor(payload, cancelled):
        assert cancelled()
        raise backend.media_operations.MediaOperationStopped('Confirmed owned worker cancellation')
    monkeypatch.setattr(backend.media_operations, 'execute', cancelled_executor)
    backend.run_media_operation_task(task['id'], task['payload'])
    assert client.get(detail).json()['status'] == 'stopped'
    assert backend.require_project_manifest('preset-test')['assets'] == []
