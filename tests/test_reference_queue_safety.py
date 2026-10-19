"""External reference workers need their own receipts, never Comfy queue inference."""
import json
import time
import uuid
from pathlib import Path

import pytest

from backend import app as backend
from support import prepare


FACTS = {
    'duration_seconds': 6.0, 'width': 640, 'height': 360, 'fps': 24.0,
    'has_video': True, 'has_audio': True, 'audio_channels': 2, 'sample_rate': 48000,
}


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Private series and per-job receipt paths already contain several levels;
    # keep the fixture prefix short on Windows without altering product paths.
    return tmp_path_factory.mktemp('rq')


def count_tasks():
    with backend.DB_LOCK, backend.db() as connection:
        return connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]


def register_reference(client, base, monkeypatch, *, with_artifacts=False):
    """Upload only registers the source; it must not enqueue decomposition work."""
    monkeypatch.setattr('tools.reference_decompose.media_facts', lambda source: dict(FACTS))
    response = client.post(base + '/reverse-analyses', files={'file': ('reference.mp4', b'isolated-reference', 'video/mp4')})
    assert response.status_code == 202, response.text
    analysis = response.json()
    assert 'import_task_id' not in analysis
    project_id = base.rsplit('/', 1)[-1]
    if with_artifacts:
        document, path = backend.read_reverse_analysis(project_id, analysis['id'])
        document['artifacts'] = {'story': {'summary': 'existing candidate', 'user_status': 'unreviewed'}}
        path.write_text(json.dumps(document), encoding='utf-8')
    return analysis


def reference_import(client, base, monkeypatch):
    """Seed a legacy in-flight decomposition row.

    Explicit reference upload never creates decomposition jobs anymore. The
    reconcile/resume safety code still has to handle rows queued by an older
    build, so the fixture reproduces that frozen payload instead of the upload
    endpoint quietly restarting an auto flow.
    """
    analysis = register_reference(client, base, monkeypatch)
    project_id = base.rsplit('/', 1)[-1]
    analysis_id = analysis['id']
    analysis_path = backend.reverse_analysis_path(project_id, analysis_id) / 'analysis.json'
    source = backend.resolve_registered_path(analysis['source']['path'])
    task_id = str(uuid.uuid4())
    receipt_path = analysis_path.parent / 'import-completion.json'
    payload = {'kind': 'reference_decomposition', 'project_id': project_id,
               'asset_id': f'reference-import-{analysis_id}', 'analysis_id': analysis_id,
               'source': str(source), 'analysis_path': str(analysis_path),
               'receipt_path': str(receipt_path), 'command': []}
    if backend.PRIVATE_WORKSPACES is not None:
        backend.PRIVATE_WORKSPACES.freeze_owner(payload, project_id)
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
                           (task_id, payload['asset_id'], 'needs_reconcile', now, now, json.dumps(payload, ensure_ascii=False), project_id))
        connection.commit()
    return backend.get_task(task_id)


def completion(task, **overrides):
    payload = task['payload']
    receipt = {'task_id': task['id'], 'source': str(Path(payload['source']).resolve()),
               'analysis_path': str(Path(payload['analysis_path']).resolve()), 'status': 'succeeded'}
    receipt.update(overrides)
    Path(payload['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
    return receipt


def semantic_task(client, base, tmp_path, monkeypatch):
    imported = register_reference(client, base, monkeypatch, with_artifacts=True)
    project_id = base.rsplit('/', 1)[-1]
    document, path = backend.read_reverse_analysis(project_id, imported['id'])
    # 显式观察才允许启动模型：登记与等距采样本身都不是镜头。
    document['timeline'] = [{'id': 'R01', 'frames': [
        {'time_seconds': 0, 'path': imported['source']['path']}], 'is_shot_cut': False}]
    path.write_text(json.dumps(document), encoding='utf-8')
    for name in ('PRIMARY_MODEL', 'PRIMARY_MMPROJ'):
        model = tmp_path / name
        model.write_bytes(b'fake-model-marker-not-a-model')
        monkeypatch.setattr(backend, name, model)
    response = client.post(base + '/reverse-analyses/' + imported['id'] + '/semantic-jobs')
    assert response.status_code == 202, response.text
    job_id = response.json()['job']['id']
    task = backend.get_task(job_id)
    backend.set_task(job_id, status='needs_reconcile')
    backend.background_jobs.update(backend.analysis_job_root(), task['payload'], status='needs_reconcile', owner='old-service')
    return backend.get_task(job_id)


@pytest.mark.parametrize('kind', ['reference_decomposition', 'reference_semantic'])
def test_empty_comfy_queue_cannot_release_unknown_external_reference(queued_private_app, tmp_path, monkeypatch, kind):
    a, _, _, _, _ = queued_private_app
    _, base = prepare(a, ['S01'])
    task = semantic_task(a, base, tmp_path, monkeypatch) if kind == 'reference_semantic' else reference_import(a, base, monkeypatch)
    before = count_tasks()
    calls = []
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: calls.append(path) or {'queue_running': [], 'queue_pending': []})
    monkeypatch.setattr(backend, 'run_reference_command', lambda *args, **kwargs: pytest.fail('Unknown reference was relaunched'))
    response = a.post(base + '/tasks/' + task['id'] + '/resolve-missing',
                      json={'confirm_execution_ended': True, 'note': 'ComfyUI is empty'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'external_execution_unresolved'
    assert backend.get_task(task['id'])['status'] == 'needs_reconcile'
    assert count_tasks() == before
    assert calls == []


def test_decomposition_completion_recovers_only_original_task(queued_private_app, monkeypatch):
    a, _, _, _, _ = queued_private_app
    _, base = prepare(a, ['S01'])
    task = reference_import(a, base, monkeypatch)
    completion(task)
    monkeypatch.setattr(backend, 'run_reference_command', lambda *args, **kwargs: pytest.fail('Receipt recovery launched a process'))
    before = count_tasks()
    response = a.post(base + '/tasks/' + task['id'] + '/reconcile')
    assert response.status_code == 200, response.text
    assert response.json()['id'] == task['id']
    assert response.json()['status'] == 'succeeded'
    assert response.json()['result']['project_id'] == task['project_id']
    assert a.post(base + '/tasks/' + task['id'] + '/reconcile').json()['id'] == task['id']
    assert count_tasks() == before


@pytest.mark.parametrize('case', ['missing', 'wrong_task', 'wrong_source', 'wrong_analysis', 'wrong_status', 'list', 'null', 'malformed'])
def test_decomposition_missing_or_mismatched_receipt_stays_uncertain(queued_private_app, monkeypatch, case):
    a, _, _, _, _ = queued_private_app
    _, base = prepare(a, ['S01'])
    task = reference_import(a, base, monkeypatch)
    before = count_tasks()
    receipt_path = Path(task['payload']['receipt_path'])
    if case in {'wrong_task', 'wrong_source', 'wrong_analysis', 'wrong_status'}:
        changes = {'wrong_task': {'task_id': 'another-task'}, 'wrong_source': {'source': 'another-source'},
                   'wrong_analysis': {'analysis_path': 'another-analysis'}, 'wrong_status': {'status': 'running'}}[case]
        completion(task, **changes)
    elif case != 'missing':
        receipt_path.write_text({'list': '[]', 'null': 'null', 'malformed': '{partial'}[case], encoding='utf-8')
    original = Path(task['payload']['analysis_path']).read_bytes()
    response = a.post(base + '/tasks/' + task['id'] + '/reconcile')
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] in {'reference_receipt_unknown', 'reference_receipt_mismatch'}
    assert backend.get_task(task['id'])['status'] == 'needs_reconcile'
    assert count_tasks() == before
    assert Path(task['payload']['analysis_path']).read_bytes() == original


@pytest.mark.parametrize('kind', ['reference_decomposition', 'reference_semantic'])
@pytest.mark.parametrize('status', ['stopped', 'failed', 'needs_reconcile'])
def test_reference_resume_without_receipt_never_blindly_clones(queued_private_app, tmp_path, monkeypatch, kind, status):
    a, _, _, _, _ = queued_private_app
    _, base = prepare(a, ['S01'])
    task = semantic_task(a, base, tmp_path, monkeypatch) if kind == 'reference_semantic' else reference_import(a, base, monkeypatch)
    backend.set_task(task['id'], status=status)
    before = count_tasks()
    monkeypatch.setattr(backend, 'run_reference_command', lambda *args, **kwargs: pytest.fail('Resume duplicated unknown reference worker'))
    response = a.post(base + '/tasks/' + task['id'] + '/resume')
    assert response.status_code == 409, response.text
    assert backend.get_task(task['id'])['status'] == status
    assert count_tasks() == before


def test_semantic_receipt_recovery(queued_private_app, tmp_path, monkeypatch):
    a, _, _, _, _ = queued_private_app
    _, base = prepare(a, ['S01'])
    task = semantic_task(a, base, tmp_path, monkeypatch)
    payload = task['payload']
    assert Path(payload['analysis_path']).name.startswith('semantic-input-')
    canonical, path = backend.read_reverse_analysis(task['project_id'], payload['analysis_id'])
    canonical['artifacts']['story'].update(summary='human-confirmed latest edit', user_status='reviewed')
    path.write_text(json.dumps(canonical), encoding='utf-8')
    result = Path(payload['result_path'])
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(json.dumps({'artifacts': {'story': {'summary': 'model candidate', 'evidence': []}}}), encoding='utf-8')
    Path(payload['receipt_path']).write_text(json.dumps({'job_id': task['id'], 'status': 'succeeded',
                                                       'analysis_path': str(Path(payload['analysis_path']).resolve()),
                                                       'result_path': str(result.resolve())}), encoding='utf-8')
    monkeypatch.setattr(backend, 'run_reference_command', lambda *args, **kwargs: pytest.fail('Semantic recovery launched model'))
    before = count_tasks()
    response = a.post(base + '/tasks/' + task['id'] + '/reconcile')
    assert response.status_code == 200, response.text
    assert response.json()['id'] == task['id'] and response.json()['status'] == 'succeeded'
    saved, _ = backend.read_reverse_analysis(task['project_id'], payload['analysis_id'])
    assert saved['artifacts']['story']['summary'] == 'human-confirmed latest edit'
    assert saved['artifacts']['story']['model_candidate']['summary'] == 'model candidate'
    assert count_tasks() == before
