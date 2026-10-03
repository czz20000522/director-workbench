"""Isolated reproductions of unresolved full-workflow reliability defects."""
import json

import pytest

from backend import app as backend
from test_production_presets import preset_client, add_shot


def test_submission_freezes_previous_assembly_identity(preset_client, monkeypatch):
    client, project = preset_client
    add_shot(client, project, 'S01')
    assert client.post('/api/projects/preset-test/reviews', json={
        'asset_id': 'S01', 'status': 'approved', 'expected_revision': 0,
    }).status_code == 200
    manifest = backend.require_project_manifest('preset-test')
    plan = backend.resolve_registered_path(manifest['plan_path'])
    document = json.loads(plan.read_text(encoding='utf-8'))
    video = project / 'workspaces/preset-test/assets/S01.mp4'
    video.write_bytes(b'current video fixture')
    document['segments'][0]['video'] = {'path': str(video)}
    previous = {'prompt_id': 'prior-prompt', 'output': 'output/prior.mp4'}
    document['assembly'] = previous
    plan.write_text(json.dumps(document), encoding='utf-8')
    graph = project / 'empty-assembly.json'
    graph.write_text('{}', encoding='utf-8')
    builder = backend.assembly_builder_path(manifest)
    builder.parent.mkdir(parents=True, exist_ok=True)
    builder.write_text('# Isolated builder availability fixture; snapshot generation is replaced below.\n', encoding='utf-8')
    monkeypatch.setattr(backend, 'build_assembly_snapshot', lambda _: graph)
    response = client.post('/api/projects/preset-test/assembly', json={'idempotency_key': 'freeze-prior'})
    assert response.status_code == 200, response.text
    assert response.json()['payload']['previous_assembly_candidate'] == previous


def test_new_assembly_requires_new_final_review(preset_client, monkeypatch):
    client, _ = preset_client
    manifest = backend.require_project_manifest('preset-test')
    plan = backend.resolve_registered_path(manifest['plan_path'])
    plan.write_text(json.dumps({'segments': [], 'assembly': {'output': 'output/old.mp4', 'prompt_id': 'old'}}), encoding='utf-8')
    asset_id = manifest['assembly_asset_id']
    response = client.post('/api/projects/preset-test/reviews', json={
        'asset_id': asset_id, 'stage': 'final', 'status': 'approved',
        'candidate_ref': 'output/old.mp4', 'expected_revision': 0,
    })
    assert response.status_code == 200, response.text
    # A late completion belongs to its frozen plan even after UI selection moves.
    assert client.post('/api/projects/create', json={
        'series': '测试', 'title': '另一作品', 'project_id': 'other-work',
    }).status_code == 200
    monkeypatch.setattr(backend, 'CURRENT_PROJECT_ID', 'other-work')
    monkeypatch.setattr(backend, 'CURRENT_PROJECT', backend.require_project_manifest('other-work'))
    catalog = backend.load_project_catalog()
    broken = backend.PROJECTS_ROOT / 'broken.json'
    broken.write_text(json.dumps({'id': 'broken'}), encoding='utf-8')
    catalog['projects'][:0] = [
        {'id': 'missing', 'manifest': 'absent.json'},
        {'id': 'broken', 'manifest': 'broken.json'},
    ]
    backend.PROJECT_CATALOG_PATH.write_text(json.dumps(catalog), encoding='utf-8')
    monkeypatch.setattr(backend, 'assembly_duration', lambda _: 5.0)
    backend.record_assembly_result('new-prompt', 'snapshot.json', {'outputs': ['new.mp4']}, plan)
    assert json.loads(plan.read_text(encoding='utf-8'))['assembly']['output'] == 'output/new.mp4'
    state = backend.build_project_state('preset-test')
    assert state['reviews'][asset_id]['final']['status'] != 'approved'
    assert state['reviews'][asset_id]['final']['candidate_ref'] == 'output/old.mp4'
    assert not backend.build_project_state('other-work')['reviews']
    stale_revision = state['reviews'][asset_id]['final']['revision']
    assert client.post('/api/projects/preset-test/reviews', json={
        'asset_id': asset_id, 'stage': 'final', 'status': 'approved',
        'candidate_ref': 'output/new.mp4', 'expected_revision': stale_revision,
    }).status_code == 200
    backend.record_assembly_result('new-prompt', 'snapshot.json', {'outputs': ['new.mp4']}, plan)
    assert backend.build_project_state('preset-test')['reviews'][asset_id]['final']['status'] == 'approved'


@pytest.mark.parametrize('previous', [
    {'prompt_id': 'old-prompt', 'output': 'output/old.mp4'},
    {'prompt_id': 'different-prompt', 'output': 'output/old.mp4'},
    None,
])
def test_reassembly_receipt_recovers_over_previous_candidate(tmp_path, monkeypatch, previous):
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'segments': [], 'assembly': {
        'prompt_id': 'old-prompt', 'output': 'output/old.mp4',
    }}), encoding='utf-8')
    (tmp_path / 'new.mp4').write_bytes(b'output verified separately')
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(backend, 'request_json', lambda _: {'queue_running': [], 'queue_pending': []})
    monkeypatch.setattr(backend, 'history_done', lambda _: {'status': 'success', 'outputs': ['new.mp4']})
    monkeypatch.setattr(backend, 'assembly_duration', lambda _: 5.0)
    monkeypatch.setattr(backend, 'set_task', lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, 'get_task', lambda _: None)
    task = {'id': 'new-task', 'asset_id': 'FINAL', 'prompt_id': 'new-prompt', 'payload': {
        'plan_path': str(plan), 'assembly_asset_id': 'FINAL', 'workflow': 'new-snapshot.json',
    }}
    if previous is not None:
        task['payload']['previous_assembly_candidate'] = previous
    if previous is None or previous['prompt_id'] != 'old-prompt':
        with pytest.raises(backend.HTTPException) as error:
            backend.reconcile_task(task)
        assert error.value.status_code == 409
        assert json.loads(plan.read_text(encoding='utf-8'))['assembly']['prompt_id'] == 'old-prompt'
        return
    backend.reconcile_task(task)
    assert json.loads(plan.read_text(encoding='utf-8'))['assembly']['prompt_id'] == 'new-prompt'
