import threading
import pytest
from concurrent.futures import ThreadPoolExecutor
from backend import app as backend
from test_production_presets import preset_client

REAL_THREAD = threading.Thread
PATH = '/api/projects/preset-test/plan'


@pytest.mark.parametrize('requested', ['scene/shot', 'scene\\shot', '镜头' * 60])
def test_duplicate_created_ids_remain_addressable(preset_client, requested):
    client, _ = preset_client
    ids = []
    for _ in range(3):
        response = client.post(PATH + '/segments', json={'segment_id': requested, 'duration_seconds': 5})
        assert response.status_code == 200, response.text
        segment_id = response.json()['segment']['id']
        assert len(segment_id) <= 80
        assert all(char.isalnum() or char in '-_' for char in segment_id)
        assert segment_id not in ids
        ids.append(segment_id)
        edited = client.patch(PATH + '/segments/' + segment_id, json={'prompt': '可继续编辑'})
        assert edited.status_code == 200, edited.text
        assert edited.json()['segment']['id'] == segment_id
    assert len(client.get(PATH).json()['segments']) == 3


def create(client):
    result = client.post(PATH + '/segments', json={'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'original'})
    assert result.status_code == 200
    return result.json()['plan']['revision']


def test_same_revision_allows_only_one_concurrent_edit(preset_client, monkeypatch):
    client, _ = preset_client
    revision = create(client)
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    barrier = threading.Barrier(2)
    def edit(prompt):
        barrier.wait()
        return client.patch(PATH + '/segments/S01', json={'prompt': prompt, 'expected_revision': revision})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(edit, ['human edit', 'agent edit']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()['segment']['prompt'] for response in responses if response.status_code == 200)
    conflict = next(response.json()['detail'] for response in responses if response.status_code == 409)
    assert conflict['code'] == 'plan_revision_conflict' and conflict['current_revision'] == revision + 1
    result = client.get(PATH).json()
    assert result['revision'] == revision + 1 and result['segments'][0]['prompt'] == winner
    assert 'expected_revision' not in result['segments'][0]


def test_old_structural_edit_preserves_plan_and_review_records(preset_client):
    client, _ = preset_client
    revision = create(client)
    assert client.patch(PATH + '/segments/S01', json={'prompt': 'new', 'expected_revision': revision}).status_code == 200
    before = client.get(PATH).json()
    records = backend.project_records('preset-test')
    response = client.post(PATH + '/segments/S01/operate', json={'operation': 'split', 'split_at_seconds': 2, 'expected_revision': revision})
    assert response.status_code == 409
    assert client.get(PATH).json() == before
    assert backend.project_records('preset-test') == records


@pytest.mark.parametrize('initiator,target', [('S01', 'S02'), ('S02', 'S01')])
def test_merge_invalidates_both_reviews_and_updates_survivor_only(preset_client, initiator, target):
    client, _ = preset_client
    for asset in ('S01', 'S02', 'S01-unrelated'):
        result = client.post(PATH + '/segments', json={'segment_id': asset, 'duration_seconds': 5})
        assert result.status_code == 200
        backend.append_project_record('review:sample', {'stage': 'sample', 'status': 'approved', 'candidate_ref': f'{asset}.mp4'}, asset_id=asset, project_id='preset-test', source='test')
    before = backend.build_project_state('preset-test')
    revision = client.get(PATH).json()['revision']
    result = client.post(PATH + f'/segments/{initiator}/operate', json={'operation': 'merge', 'target_id': target, 'expected_revision': revision})
    assert result.status_code == 200, result.text
    assert [s['id'] for s in result.json()['segments']] == ['S01', 'S01-unrelated']
    after = backend.build_project_state('preset-test')
    for asset in ('S01', 'S02'):
        assert after['reviews'][asset]['sample']['status'] == 'stale'
        assert after['reviews'][asset]['sample']['candidate_ref'] == f'{asset}.mp4'
    assert after['reviews']['S01-unrelated'] == before['reviews']['S01-unrelated']
    survivor = [a for a in after['artifacts'] if a['asset_id'] == 'S01' and a['kind'] == 'storyboard']
    assert survivor and survivor[-1]['content']['duration_seconds'] == 10
    assert [a for a in after['artifacts'] if a['asset_id'] == 'S01-unrelated'] == [a for a in before['artifacts'] if a['asset_id'] == 'S01-unrelated']


def test_generation_result_advances_revision_without_losing_editor_content(preset_client):
    client, _ = preset_client
    revision = create(client)
    _, path = backend.load_project_plan('preset-test')
    backend.record_segment_result('S01', 'receipt', 'graph.json', {'outputs': ['sample.mp4']}, path)
    plan = client.get(PATH).json()
    assert plan['revision'] == revision + 1
    assert plan['segments'][0]['prompt'] == 'original'
    assert plan['segments'][0]['video']['path'] == 'output/sample.mp4'
    assert client.patch(PATH + '/segments/S01', json={'prompt': 'stale', 'expected_revision': revision}).status_code == 409
    assert client.patch(PATH + '/segments/S01', json={'prompt': 'merged', 'expected_revision': revision + 1}).status_code == 200
