"""Public request enums and HTTP rejection without persisted side effects."""
import pytest

from backend import app as backend
from test_production_presets import preset_client


STATES = ['draft', 'pending_review', 'approved', 'changes_requested', 'stale']
REVIEW_STATES = ['pending_review', 'approved', 'changes_requested', 'stale']


def test_openapi_lists_global_and_project_writable_review_values():
    schemas = backend.app.openapi()['components']['schemas']
    for name in ('CreativeSettingsRequest', 'ProjectCreativeSettingsRequest', 'CheckpointRequest', 'ArtifactRequest'):
        assert schemas[name]['properties']['status']['enum'] == STATES
    for name in ('ReviewRecordRequest', 'ProjectReviewRecordRequest'):
        assert schemas[name]['properties']['status']['enum'] == REVIEW_STATES
        assert schemas[name]['properties']['stage']['enum'] == ['sample', 'finish', 'final']


@pytest.mark.parametrize('resource,body,invalid', [
    ('creative', {'values': {'intent': 'synthetic test'}}, {'status': 'completed'}),
    ('reviews', {'asset_id': 'S01', 'stage': 'sample'}, {'status': 'draft'}),
    ('reviews', {'asset_id': 'S01', 'status': 'pending_review'}, {'stage': 'unknown'}),
    ('checkpoints', {'stage_id': 'intent'}, {'status': 'completed'}),
    ('artifacts', {'kind': 'intent', 'content': {}}, {'status': 'completed'}),
])
def test_invalid_contract_values_return_422_without_writes_and_valid_requests_work(preset_client, resource, body, invalid):
    client, _ = preset_client
    with backend.db() as connection:
        before = connection.execute('SELECT COUNT(*) FROM project_records').fetchone()[0]
    url = f'/api/projects/preset-test/{resource}'
    response = client.post(url, json={'expected_revision': 0, **body, **invalid})
    assert response.status_code == 422, response.text
    with backend.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM project_records').fetchone()[0] == before
    response = client.post(url, json={'expected_revision': 0, **body, 'status': 'pending_review', **({'stage': 'sample'} if resource == 'reviews' else {})})
    assert response.status_code == 200, response.text
