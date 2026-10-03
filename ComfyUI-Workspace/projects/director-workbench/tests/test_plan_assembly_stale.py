import json

import pytest

from backend import app as backend
from test_production_presets import preset_client

BASE = '/api/projects/preset-test'


@pytest.mark.parametrize('operation', ['create', 'update', 'copy', 'move', 'split', 'merge', 'merge_range', 'convert'])
def test_plan_edits_stale_assembly_preserving_candidate_and_review_history(preset_client, operation):
    client, _ = preset_client
    for shot in ('S01', 'S02'):
        assert client.post(BASE + '/plan/segments', json={'segment_id': shot, 'duration_seconds': 5}).status_code == 200
    document, path = backend.load_project_plan('preset-test')
    previous = {'status': 'candidate_generated', 'output': 'output/old.mp4', 'prompt_id': 'old-task',
                'workflow': 'old.json', 'duration_seconds': 4.999,
                'version_history': [{'output': 'older.mp4'}]}
    document['assembly'] = previous
    path.write_text(json.dumps(document), encoding='utf-8')
    original_review = {'stage': 'final', 'status': 'approved', 'candidate_ref': 'output/old.mp4', 'adopted_variant': 'A', 'note': 'original review'}
    backend.append_project_record('review:final', original_review, asset_id='MASTER', project_id='preset-test', source='test')
    revision = document['revision']
    if operation == 'create':
        response = client.post(BASE + '/plan/segments', json={'segment_id': 'S03', 'duration_seconds': 5, 'expected_revision': revision})
    elif operation == 'update':
        response = client.patch(BASE + '/plan/segments/S01', json={'duration_seconds': 4, 'expected_revision': revision})
    elif operation == 'convert':
        manifest = backend.load_project_manifest('preset-test')
        analysis = backend.resolve_registered_path(manifest['workspace_root']) / 'reference-analysis/ref-test/analysis.json'
        analysis.parent.mkdir(parents=True)
        analysis.write_text(json.dumps({'id': 'ref-test', 'project_id': 'preset-test', 'timeline': [{'id': 'R01', 'start_seconds': 0, 'end_seconds': 5}], 'artifacts': {}, 'stages': []}), encoding='utf-8')
        response = client.post(BASE + '/reverse-analyses/ref-test/convert')
    else:
        response = client.post(BASE + '/plan/segments/S01/operate', json={'operation': operation, 'expected_revision': revision, 'before_id': '__end__', 'target_id': 'S02', 'split_at_seconds': 2})
    assert response.status_code == 200, response.text
    result = client.get(BASE + '/plan').json()
    assert result['revision'] == revision + 1
    assert result['assembly']['status'] == 'stale'
    assert {key: result['assembly'][key] for key in previous if key != 'status'} == {key: value for key, value in previous.items() if key != 'status'}
    review = backend.build_project_state('preset-test')['reviews']['MASTER']['final']
    assert review['status'] == 'stale' and review['revision'] == 2
    assert review['candidate_ref'] == original_review['candidate_ref']
    assert review['adopted_variant'] == 'A'
    records = backend.project_records('preset-test')
    assert any(row['record_type'] == 'review:final' and row['data'] == original_review for row in records)
    assert client.patch(BASE + '/plan/segments/' + result['segments'][0]['id'], json={'prompt': 'further edit', 'expected_revision': revision + 1}).status_code == 200
    assert backend.build_project_state('preset-test')['reviews']['MASTER']['final']['revision'] == 2


def test_empty_project_edit_does_not_invent_assembly_or_final_review(preset_client):
    client, _ = preset_client
    result = client.post(BASE + '/plan/segments', json={'duration_seconds': 5})
    assert result.status_code == 200
    assert result.json()['plan']['assembly'] is None
    assert 'MASTER' not in backend.build_project_state('preset-test')['reviews']

@pytest.mark.parametrize('scoped', [True, False])
def test_stale_assembly_cannot_be_approved_but_accepts_feedback(preset_client, monkeypatch, scoped):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'CURRENT_PROJECT_ID', 'preset-test')
    client.post(BASE + '/plan/segments', json={'duration_seconds': 5})
    document, path = backend.load_project_plan('preset-test')
    document['assembly'] = {'status': 'stale', 'output': 'output/old.mp4'}
    path.write_text(json.dumps(document), encoding='utf-8')
    endpoint = BASE + '/reviews' if scoped else '/api/project-state/reviews'
    payload = {'asset_id': 'MASTER', 'stage': 'final', 'status': 'approved', 'expected_revision': 0}
    before = backend.project_records('preset-test')
    assert client.post(endpoint, json=payload).status_code == 409
    assert backend.project_records('preset-test') == before
    payload.update(status='changes_requested', note='Please reassemble current shots')
    assert client.post(endpoint, json=payload).status_code == 200
    payload.update(status='pending_review', expected_revision=1)
    assert client.post(endpoint, json=payload).status_code == 200
    assert json.loads(path.read_text(encoding='utf-8')) == document
