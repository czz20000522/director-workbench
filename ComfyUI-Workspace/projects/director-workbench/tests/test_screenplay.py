import pytest

from backend import app as backend
from test_production_presets import preset_client
from test_private_workbench import private_workbench, create


BASE = '/api/projects/preset-test'


def screenplay(revision=0, **extra):
    return {'expected_revision': revision, 'title': '铜钱', 'text': '一场戏并不是一个分镜。',
            'scenes': [{'id': 'SC01', 'title': '卧房', 'duration_seconds': 40},
                       {'id': 'SC02', 'title': '道路', 'text': '挑铜钱走过道路。'}], **extra}


def shot(client, scene='SC01', revision=1, shot_id='S01'):
    return client.post(BASE + '/plan/segments', json={'duration_seconds': 5, 'segment_id': shot_id,
                       'script_scene_id': scene, 'expected_script_revision': revision})


def test_legacy_script_editor_preserves_metadata_and_history(preset_client):
    client, _ = preset_client
    assert client.get(BASE + '/script').json()['revision'] == 0
    legacy = client.post(BASE + '/artifacts', json={'expected_revision': 0, 'kind': 'script',
        'title': '旧稿', 'status': 'approved', 'references': ['source.txt'],
        'content': {'text': '旧正文', 'target_duration_seconds': 180,
                    'approval_basis': '用户批准', 'scenes': [{'id': 'SC01', 'title': '卧房'}]}})
    assert legacy.status_code == 200, legacy.text
    assert client.get(BASE + '/script').json()['scenes'] == [{'id': 'SC01', 'title': '卧房'}]
    saved = client.put(BASE + '/script', json=screenplay(1))
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'draft'
    assert saved.json()['target_duration_seconds'] == 180
    records = [r for r in backend.project_records('preset-test') if r['record_type'] == 'artifact:script']
    assert len(records) == 2 and records[0]['data']['status'] == 'approved'
    assert records[-1]['data']['content']['approval_basis'] == '用户批准'
    assert records[-1]['data']['references'] == ['source.txt']
    assert client.put(BASE + '/script', json=screenplay(1, text='覆盖')).status_code == 409
    assert client.get(BASE + '/script').json()['text'] == saved.json()['text']


@pytest.mark.parametrize('scenes', [
    [{'id': '', 'title': '空'}], [{'id': ' ', 'title': '空'}],
    [{'id': 'x', 'title': '甲'}, {'id': 'x', 'title': '乙'}],
    [{'id': 'x', 'title': '甲', 'duration_seconds': 0}],
])
def test_invalid_scene_does_not_create_version(preset_client, scenes):
    client, _ = preset_client
    assert client.put(BASE + '/script', json=screenplay(scenes=scenes)).status_code == 422
    assert client.get(BASE + '/script').json()['revision'] == 0


def test_link_conflicts_and_scene_deletion_guard_on_both_write_routes(preset_client):
    client, _ = preset_client
    assert client.put(BASE + '/script', json=screenplay()).status_code == 200
    assert shot(client, revision=0).status_code == 409
    assert shot(client, scene='missing').status_code == 422
    created = shot(client)
    assert created.status_code == 200, created.text
    assert created.json()['segment']['duration_seconds'] == 5
    assert created.json()['segment']['script_revision'] == 1
    assert client.put(BASE + '/script', json=screenplay(1, scenes=[])).status_code == 409
    assert client.post(BASE + '/artifacts', json={'expected_revision': 1, 'kind': 'script',
        'content': {'text': '移除场次'}}).status_code == 409
    assert client.patch(BASE + '/plan/segments/S01', json={'script_scene_id': ''}).status_code == 422
    assert client.patch(BASE + '/plan/segments/S01', json={'script_scene_id': '',
        'expected_script_revision': 1}).status_code == 200
    assert client.put(BASE + '/script', json=screenplay(1, scenes=[])).status_code == 200


def test_copy_split_keep_links_and_cross_scene_merge_rejected(preset_client):
    client, _ = preset_client
    client.put(BASE + '/script', json=screenplay())
    assert shot(client).status_code == 200
    copied = client.post(BASE + '/plan/segments/S01/operate', json={'operation': 'copy'})
    assert copied.status_code == 200, copied.text
    split = client.post(BASE + '/plan/segments/S01/operate', json={'operation': 'split', 'split_at_seconds': 2})
    assert split.status_code == 200, split.text
    assert all(s['script_scene_id'] == 'SC01' for s in split.json()['plan']['segments'])
    assert shot(client, scene='SC02', shot_id='S02').status_code == 200
    merged = client.post(BASE + '/plan/segments/S01-copy/operate', json={'operation': 'merge', 'target_id': 'S02'})
    assert merged.status_code == 409


def test_script_routes_keep_private_project_ownership(private_workbench):
    owner, other, _, _ = private_workbench
    project = create(owner)
    path = '/api/projects/' + project['id'] + '/script'
    assert owner.put(path, json=screenplay()).status_code == 200
    assert other.get(path).status_code == 404
    assert other.put(path, json=screenplay(1)).status_code == 404
    assert owner.get(path).json()['revision'] == 1
