import json
from pathlib import Path
from backend import app as backend
import pytest
from test_reverse_workbench import reverse_client


@pytest.mark.parametrize('suffix,body', [('/artifacts/story', {'summary': '人工新意见'}), ('/timeline/R01', {'user_note': '人工新动作'})])
def test_stale_reference_edit_preserves_document(reverse_client, suffix, body):
    client, _, path = reverse_client
    url = '/api/projects/project-a/reverse-analyses/ref-test'
    saved = client.patch(url + suffix, json={**body, 'expected_revision': 0})
    assert saved.status_code == 200
    assert saved.json()['analysis']['revision'] == 1
    before = path.read_bytes()
    stale = client.patch(url + suffix, json={**body, 'expected_revision': 0})
    assert stale.status_code == 409
    assert stale.json()['detail']['code'] == 'analysis_revision_conflict'
    assert path.read_bytes() == before
    assert client.patch(url + suffix, json={**body, 'expected_revision': 1}).json()['analysis']['revision'] == 2


def test_timeline_and_artifact_share_analysis_revision(reverse_client):
    client, _, path = reverse_client
    url = '/api/projects/project-a/reverse-analyses/ref-test'
    assert client.patch(url + '/timeline/R01', json={'user_note': 'new', 'expected_revision': 0}).status_code == 200
    assert client.patch(url + '/artifacts/story', json={'summary': 'old page', 'expected_revision': 0}).status_code == 409
    assert json.loads(path.read_text(encoding='utf-8'))['artifacts']['story']['summary'] == '模型猜测'


def test_stale_conversion_has_no_plan_seed_or_archive_writes(reverse_client):
    client, manifest, path = reverse_client
    url = '/api/projects/project-a/reverse-analyses/ref-test'
    assert client.patch(url + '/timeline/R01', json={'user_note': '另一位导演的新校订', 'expected_revision': 0}).status_code == 200
    before = path.read_bytes()
    records = backend.project_records('project-a')
    files = set(path.parent.rglob('*'))
    response = client.post(url + '/convert', json={'expected_revision': 0})
    assert response.status_code == 409
    assert response.json()['detail']['current_revision'] == 1
    assert path.read_bytes() == before
    assert set(path.parent.rglob('*')) == files
    assert not Path(manifest['plan_path']).exists()
    assert backend.project_records('project-a') == records
    converted = client.post(url + '/convert', json={'expected_revision': 1})
    assert converted.status_code == 200
    assert converted.json()['created_segments'][0]['performance'] == '另一位导演的新校订'
    assert converted.json()['created_segments'][0]['source_revision'] == 1
