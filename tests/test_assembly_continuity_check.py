"""Cross-shot continuity check: explicit inputs, explainable blockers, read-only.

The unit cases build the report from already collected facts; the HTTP cases use
the isolated `edit_client` fixture (synthetic media, no GPU, no scheduler work).
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend import app as backend
from backend import assembly_continuity_check as check


def _segment(segment_id, *, duration=5, start=None, end=None, video='S01.mp4', task='task'):
    row = {'id': segment_id, 'duration_seconds': duration,
           'video': {'path': video} if video else {}, 'current_version_task_id': task}
    if start is not None:
        row['start_seconds'] = start
    if end is not None:
        row['end_seconds'] = end
    return row


def _join(left, right, *, status='pending', ready=True, completed=False):
    return {'key': f'{left}>{right}', 'left_id': left, 'right_id': right, 'status': status,
            'review_ready': ready, 'review_completed': completed}


def _codes(report, severity=None):
    return [item['code'] for item in report['checks']
            if severity is None or item['severity'] == severity]


# -- pure report -----------------------------------------------------------------

def test_report_blocks_unselected_sound_and_missing_adoption_without_repairing():
    document = {'revision': 4, 'segments': [
        _segment('S01', start=0, end=5), _segment('S02', start=5, end=10)]}
    adoption = [{'code': 'adoption_required', 'dependency': 'S01', 'message': 'S01 尚未有明确采用记录。'}]
    joins = [_join('S01', 'S02')]
    report = check.build_report({'id': 'p1'}, document, adoption_blockers=adoption, joins=joins,
                                join_tracking='legacy_untracked',
                                sound={'policy': 'segment_native', 'selected': False,
                                       'selection_source': None},
                                sound_blockers=[{'code': 'sound_policy_required', 'message': '须选择音轨方式。'}])
    assert report['read_only'] is True and report['automatic_actions'] is False
    assert report['ready'] is False
    assert set(_codes(report, 'blocker')) == {'adoption_required', 'sound_policy_required'}
    # The legacy project has no assembly_edit record: unreviewed joins stay warnings.
    assert [item['severity'] for item in report['checks'] if item['code'] == 'join_review_untracked'] == ['warning']
    assert report['inputs']['sound'] == {'selected': False, 'policy': 'segment_native',
                                         'selection_source': None}
    # A pure report never mutates the caller's document.
    assert document['segments'][0]['video'] == {'path': 'S01.mp4'}


def test_report_tracks_join_status_after_explicit_review_flow_starts():
    document = {'revision': 9, 'assembly_edit': {'sound': {'policy': 'mute', 'audio_asset_id': None}},
                'segments': [_segment('S01', start=0, end=5), _segment('S02', start=5, end=10)]}
    report = check.build_report({'id': 'p1'}, document,
                                joins=[_join('S01', 'S02', status='stale')],
                                sound={'policy': 'mute', 'selected': True, 'selection_source': 'assembly_edit'})
    assert 'join_review_stale' in _codes(report, 'blocker')
    passed = check.build_report({'id': 'p1'}, document,
                                joins=[_join('S01', 'S02', status='approved', completed=True)],
                                sound={'policy': 'mute', 'selected': True, 'selection_source': 'assembly_edit'})
    assert passed['blockers'] == [] and passed['ready'] is True
    assert 'sound_policy' in _codes(passed, 'pass')
    assert 'join_reviews' in _codes(passed, 'pass')


def test_report_detects_timing_changes_and_duplicate_order():
    drifting = {'revision': 2, 'segments': [
        _segment('S01', start=0.5, end=5.5), _segment('S02', start=5.5, end=10.5)]}
    report = check.build_report({'id': 'p1'}, drifting, join_tracking='legacy_untracked')
    assert 'segment_timing_mismatch' in _codes(report, 'blocker')
    duplicated = {'revision': 2, 'segments': [_segment('S01'), _segment('S01')]}
    invalid = check.build_report({'id': 'p1'}, duplicated)
    assert 'segment_order_invalid' in _codes(invalid, 'blocker')
    empty = check.build_report({'id': 'p1'}, {'segments': []})
    assert _codes(empty, 'blocker') == ['segments_required'] and empty['ready'] is False


def test_report_maps_existing_video_duration_blocker_as_media_blocker():
    report = check.build_report({'id': 'p1'}, {'segments': [_segment('S01')]},
                                video_blockers=[{'code': 'video_duration_stale', 'dependency': 'S01',
                                                 'message': 'S01 当前视频按旧时长生成'}])
    item = next(row for row in report['checks'] if row['code'] == 'video_duration_stale')
    assert item['severity'] == 'blocker' and item['category'] == 'media'
    assert item['evidence'] == {'segment_id': 'S01'}


def test_report_reference_difference_is_explained_warning_without_paths():
    document = {'revision': 1, 'segments': [
        {**_segment('S01', video='/registered/internal/S01.mp4'), 'reference_asset_id': 'ref-video'}]}

    def resolver(segment):
        assert segment['reference_asset_id'] == 'ref-video'
        return {'asset_id': 'ref-video', 'facts': {'known': True, 'width': 1920, 'height': 1080,
                                                   'fps': 24.0, 'duration_seconds': 4.0}}

    report = check.build_report({'id': 'p1'}, document,
                                reference_resolver=resolver,
                                media_facts=lambda path: {'known': True, 'width': 1280, 'height': 720,
                                                          'fps': 24.0, 'duration_seconds': 4.0})
    item = next(row for row in report['checks'] if row['code'] == 'reference_current_difference')
    assert item['severity'] == 'warning' and item['category'] == 'reference'
    assert {row['field'] for row in item['evidence']['differences']} == {'width', 'height'}
    assert '/registered/internal' not in json.dumps(report, ensure_ascii=False)
    assert report['blockers'] == []
    assert report['inputs']['reference_bindings'] == [
        {'segment_id': 'S01', 'kind': 'asset', 'id': 'ref-video'}]


def test_report_marks_unmeasurable_reference_instead_of_claiming_mismatch():
    document = {'revision': 1, 'segments': [
        {**_segment('S01'), 'source_analysis': 'ref-analysis'}]}
    report = check.build_report({'id': 'p1'}, document,
                                reference_resolver=lambda segment: {'facts': {'known': False,
                                                                             'reason': 'media_unavailable'}},
                                media_facts=lambda path: {'known': False, 'reason': 'metadata_unavailable'})
    item = next(row for row in report['checks'] if row['code'] == 'reference_current_unmeasured')
    assert item['severity'] == 'warning'
    assert item['evidence']['reference_reason'] == 'media_unavailable'


# -- HTTP integration ------------------------------------------------------------

def test_http_check_is_read_only_and_exposes_missing_sound_policy(edit_client):
    client, root, _workspace, plan_path = edit_client
    before_plan = client.get(root + '/plan').json()
    before_bytes = plan_path.read_bytes()
    before_tasks = client.get(root + '/tasks').json()['tasks']
    response = client.get(root + '/assembly/continuity-check')
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['read_only'] is True and report['automatic_actions'] is False
    assert report['revision'] == before_plan['revision']
    assert 'sound_policy_required' in [item['code'] for item in report['blockers']]
    # The fixture adopts explicitly, so adoption passes and unreviewed joins stay legacy warnings.
    assert 'adoption' in [item['code'] for item in report['checks'] if item['severity'] == 'pass']
    assert [item['severity'] for item in report['checks'] if item['code'] == 'join_review_untracked'] == ['warning']
    # The explicit endpoint matches the embedded report and writes nothing.
    edit = client.get(root + '/assembly/edit').json()
    assert edit['continuity_check'] == report
    assert client.get(root + '/plan').json() == before_plan
    assert plan_path.read_bytes() == before_bytes
    assert client.get(root + '/tasks').json()['tasks'] == before_tasks


def test_http_check_blocks_untracked_join_then_passes_after_explicit_reviews(edit_client):
    client, root, _, _ = edit_client
    current = client.get(root + '/plan').json()
    saved = client.put(root + '/assembly/sound', json={
        'expected_revision': current['revision'], 'policy': 'segment_native'})
    assert saved.status_code == 200, saved.text
    pending = client.get(root + '/assembly/continuity-check').json()
    assert 'join_review_required' in [item['code'] for item in pending['blockers']]
    assert pending['ready'] is False
    approved = client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': pending['revision'], 'status': 'approved', 'note': 'isolated explicit review'})
    assert approved.status_code == 200, approved.text
    passed = client.get(root + '/assembly/continuity-check').json()
    assert passed['blockers'] == []
    assert passed['ready'] is True
    assert 'join_reviews' in [item['code'] for item in passed['checks'] if item['severity'] == 'pass']
    assert passed['inputs']['sound']['selected'] is True


def test_http_check_reports_input_change_and_timing_drift(edit_client):
    client, root, workspace, plan_path = edit_client
    current = client.get(root + '/plan').json()
    assert client.put(root + '/assembly/sound', json={
        'expected_revision': current['revision'], 'policy': 'mute'}).status_code == 200
    edit = client.get(root + '/assembly/edit').json()
    assert client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': edit['revision'], 'status': 'approved'}).status_code == 200
    document = json.loads(plan_path.read_text(encoding='utf-8'))
    (workspace / 'S02-next.mp4').write_bytes(b'new synthetic version')
    document['segments'][1]['video'] = {'path': str(workspace / 'S02-next.mp4')}
    document['segments'][1]['current_version_task_id'] = 'task-S02-next'
    backend.write_plan_version(plan_path, document)
    changed = client.get(root + '/assembly/continuity-check').json()
    blocker_codes = {item['code'] for item in changed['blockers']}
    assert 'join_review_stale' in blocker_codes
    assert any(item['category'] == 'adoption' for item in changed['blockers'])
    assert changed['ready'] is False
    serialized = json.dumps(changed, ensure_ascii=False)
    assert str(workspace) not in serialized
    document = json.loads(plan_path.read_text(encoding='utf-8'))
    document['segments'][1]['start_seconds'] = 6.5
    backend.write_plan_version(plan_path, document)
    drifted = client.get(root + '/assembly/continuity-check').json()
    assert 'segment_timing_mismatch' in {item['code'] for item in drifted['blockers']}
    assert plan_path.is_file() and (workspace / 'S02-next.mp4').is_file()


def test_http_check_reports_missing_video_without_rendering(edit_client):
    client, root, workspace, plan_path = edit_client
    current = client.get(root + '/plan').json()
    assert client.put(root + '/assembly/sound', json={
        'expected_revision': current['revision'], 'policy': 'segment_native'}).status_code == 200
    document = json.loads(plan_path.read_text(encoding='utf-8'))
    document['segments'][1]['video'] = {'path': str(workspace / 'missing.mp4')}
    backend.write_plan_version(plan_path, document)
    report = client.get(root + '/assembly/continuity-check').json()
    assert 'video_required' in {item['code'] for item in report['blockers']}
    assert report['ready'] is False


def test_assembly_submission_is_blocked_by_unselected_sound_without_creating_work(edit_client):
    client, root, *_ = edit_client
    before = client.get(root + '/plan').json()
    before_tasks = client.get(root + '/tasks').json()['tasks']
    response = client.post(root + '/assembly', json={'idempotency_key': 'issue1-sound-gate'})
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'assembly_continuity_blocked'
    assert 'sound_policy_required' in {item['code'] for item in detail['blockers']}
    assert client.get(root + '/plan').json() == before
    assert client.get(root + '/tasks').json()['tasks'] == before_tasks


def test_assembly_submission_accepts_explicit_sound_and_reviewed_join(preset_client):
    client, project = preset_client
    builder = Path(__file__).resolve().parents[1] / 'tools' / 'build_assembly_workflow.py'
    manifest = backend.load_project_manifest('preset-test')
    manifest['assembly']['builder'] = str(builder)
    backend.save_project_manifest(manifest)
    for shot_id in ('S01', 'S02'):
        assert client.post('/api/projects/preset-test/plan/segments', json={
            'segment_id': shot_id, 'duration_seconds': 5, 'workflow': 'workflows/planned.json'}).status_code == 200
    document, plan_path = backend.load_project_plan('preset-test')
    manifest = backend.load_project_manifest('preset-test')
    workspace = backend.resolve_registered_path(manifest['workspace_root'])
    workspace.mkdir(parents=True, exist_ok=True)
    for segment in document['segments']:
        video = workspace / f"{segment['id']}.mp4"
        video.write_bytes(b'isolated synthetic candidate')
        segment['video'] = {'path': str(video)}
        segment['current_version_task_id'] = f"task-{segment['id']}"
    backend.write_plan_version(plan_path, document)
    document, _ = backend.load_project_plan('preset-test')
    for segment in document['segments']:
        adopted = client.post('/api/projects/preset-test/reviews', json={
            'asset_id': segment['id'], 'stage': 'sample', 'status': 'approved',
            'candidate_ref': segment['video']['path'], 'expected_revision': 0,
            'expected_plan_revision': document['revision'],
            'note': 'Explicit isolated adoption for the continuity pass path.',
            'source': 'explicit-adoption'})
        assert adopted.status_code == 200, adopted.text
    revision = client.get('/api/projects/preset-test/plan').json()['revision']
    assert client.put('/api/projects/preset-test/assembly/sound', json={
        'expected_revision': revision, 'policy': 'segment_native'}).status_code == 200
    edit = client.get('/api/projects/preset-test/assembly/edit').json()
    assert client.put('/api/projects/preset-test/assembly/joins/S01/S02', json={
        'expected_revision': edit['revision'], 'status': 'approved',
        'note': 'isolated explicit join review'}).status_code == 200
    checked = client.get('/api/projects/preset-test/assembly/continuity-check').json()
    assert checked['blockers'] == [] and checked['ready'] is True
    revision = checked['revision']
    submitted = client.post('/api/projects/preset-test/assembly', json={
        'expected_revision': revision, 'idempotency_key': 'issue1-continuity-pass'})
    assert submitted.status_code == 200, submitted.text
    task = submitted.json()
    assert task['payload']['assembly_edit']['sound']['policy'] == 'segment_native'
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_mcp_check_matches_http_contract(edit_client):
    pytest.importorskip('mcp')
    from mcp import Client
    from mcp_server.server import create_server

    client, root, _, _ = edit_client

    async def verify():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app))
        async with Client(server) as agent:
            result = (await agent.call_tool('check_assembly_continuity',
                                            {'project_id': 'edit-test'})).structured_content
            assert result['ok']
            assert result['data'] == client.get(root + '/assembly/continuity-check').json()
            assert result['data']['read_only'] is True

    asyncio.run(verify())
