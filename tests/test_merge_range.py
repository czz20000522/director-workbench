import json

import pytest

from backend import app as backend
from test_production_presets import preset_client


@pytest.fixture
def range_plan(preset_client):
    client, _ = preset_client
    manifest = backend.load_project_manifest('preset-test')
    path = backend.resolve_registered_path(manifest['plan_path'])
    segments = [{'id': f'S{i}', 'duration_seconds': 1, 'prompt': f'prompt{i}',
                 'source_analysis': 'reference', 'source_segment': f'R{i}',
                 'source_range_seconds': [i - 1, i],
                 'dependencies': [f'S{i-1}'] if i > 1 else []} for i in range(1, 5)]
    segments[0]['video'] = {'path': 'first.mp4'}
    segments[1]['video'] = {'path': 'second.mp4'}
    path.write_text(json.dumps({'revision': 7, 'segments': segments}), encoding='utf-8')
    return client, path


def test_range_merge_one_revision_preserves_sources_and_candidates(range_plan):
    client, path = range_plan
    response = client.post('/api/projects/preset-test/plan/segments/S1/operate', json={
        'operation': 'merge_range', 'target_id': 'S3', 'expected_revision': 7,
    })
    assert response.status_code == 200, response.text
    plan = response.json()['plan']
    assert plan['revision'] == 8
    first, following = plan['segments']
    assert first['id'] == 'S1' and first['duration_seconds'] == 3
    assert first['dependencies'] == []
    assert following['dependencies'] == ['S1']
    assert 'video' not in first
    assert first['version_history'][0]['video']['path'] == 'first.mp4'
    merged = [entry['merged_segment'] for entry in first['version_history'] if 'merged_segment' in entry]
    assert [item['source_segment'] for item in merged] == ['R2', 'R3']
    assert merged[0]['video']['path'] == 'second.mp4'
    before = path.read_bytes()
    assert client.post('/api/projects/preset-test/plan/segments/S1/operate', json={
        'operation': 'merge_range', 'target_id': 'S4', 'expected_revision': 7,
    }).status_code == 409
    assert path.read_bytes() == before


@pytest.mark.parametrize('body', [
    {'operation': 'merge_range', 'target_id': 'S3'},
    {'operation': 'merge_range', 'target_id': 'S1', 'expected_revision': 7},
    {'operation': 'merge_range', 'target_id': 'missing', 'expected_revision': 7},
    {'operation': 'merge', 'target_id': 'S3', 'expected_revision': 7},
])
def test_invalid_range_never_writes(range_plan, body):
    client, path = range_plan
    before = path.read_bytes()
    response = client.post('/api/projects/preset-test/plan/segments/S1/operate', json=body)
    assert response.status_code == 422
    assert path.read_bytes() == before
