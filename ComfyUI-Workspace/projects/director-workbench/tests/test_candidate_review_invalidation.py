"""Candidate identity checks with real SQLite and isolated receipt handlers.

The tiny files are synthetic fixtures, not generated or visually accepted media.
No executor, ComfyUI service, or GPU job is started by these checks.
"""
import json
from urllib.parse import quote

import pytest

from backend import app as backend
from backend.user_context import UserContext
from test_private_workbench import create, private_workbench
from test_production_presets import add_shot, preset_client


BASE = '/api/projects/preset-test'


def complete_candidate(client, project, task_id, *, path=None, when=100):
    document, plan_path = backend.load_project_plan('preset-test')
    segment = document['segments'][0]
    video = path or project / f'workspaces/preset-test/assets/{task_id}.mp4'
    video.write_bytes(b'isolated candidate ' + task_id.encode())
    payload = {'project_id': 'preset-test', 'asset_id': 'S01',
               'pipeline_stage_id': 'motion-generation',
               'segment_snapshot': backend.segment_generation_snapshot(segment)}
    result = {'status': 'success', 'outputs': [video.name], 'published_outputs': [str(video)]}
    with backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id,prompt_id) VALUES (?,?,?,?,?,?,?,?,?)',
            (task_id, 'S01', 'succeeded', when - 1, when, json.dumps(payload),
             json.dumps(result), 'preset-test', task_id),
        )
        connection.commit()
    backend.record_segment_result('S01', task_id, 'frozen-fixture.json', result, plan_path,
                                  task_id=task_id, payload=payload, generated_at=when)
    return video


def approve(client, video, stage='sample', **changes):
    document, _ = backend.load_project_plan('preset-test')
    existing = backend.build_project_state('preset-test')['reviews'].get('S01', {}).get(stage, {})
    return client.post(BASE + '/reviews', json={
        'asset_id': 'S01', 'stage': stage, 'status': 'approved',
        'candidate_ref': '/media-file?path=' + quote(str(video), safe=''),
        'expected_revision': existing.get('revision', 0),
        'expected_plan_revision': document['revision'],
        'note': 'Explicit isolated selection; no human viewing claim.', **changes,
    })


def state(client):
    response = client.get(BASE + '/state')
    assert response.status_code == 200, response.text
    return response.json()


def guide_review(client):
    response = client.get('/api/settings/guide', params={'project_id': 'preset-test'})
    assert response.status_code == 200, response.text
    return next(row for row in response.json()['steps'] if row['id'] == 'review')


@pytest.mark.parametrize('change', ['generation', 'restore'])
@pytest.mark.parametrize('stage', ['sample', 'finish'])
def test_candidate_change_stales_reviews_and_guide_but_preserves_history(preset_client, change, stage):
    client, project = preset_client
    add_shot(client, project, 'S01')
    first = complete_candidate(client, project, 'first')
    second = complete_candidate(client, project, 'second', when=200) if change == 'restore' else None
    reviewed = second or first
    assert approve(client, reviewed).status_code == 200
    if stage == 'finish':
        assert approve(client, reviewed, stage).status_code == 200
    before = state(client)
    assert before['reviews']['S01'][stage]['status'] == 'approved'
    assert before['readiness']['segments'][0]['state'] == 'adopted'
    assert guide_review(client)['completed']
    old_bytes = reviewed.read_bytes()

    if change == 'generation':
        current = complete_candidate(client, project, 'second', when=200)
    else:
        revision = client.get(BASE + '/plan').json()['revision']
        response = client.post(BASE + '/segments/S01/versions/first/restore',
                               json={'expected_plan_revision': revision})
        assert response.status_code == 200, response.text
        current = first

    after = state(client)
    for checked_stage in ('sample', 'finish') if stage == 'finish' else ('sample',):
        review = after['reviews']['S01'][checked_stage]
        assert review['status'] == 'stale'
        assert backend.adoption_candidate_path(review['candidate_ref']) == reviewed
        assert review['revision'] == before['reviews']['S01'][checked_stage]['revision'] + 1
        history = [row for row in after['history'] if row['record_type'] == 'review:' + checked_stage]
        assert [row['data']['status'] for row in history] == ['approved', 'stale']
        assert history[0]['data']['note'] == 'Explicit isolated selection; no human viewing claim.'
    assert after['readiness']['segments'][0]['state'] == 'review_stale'
    assert not guide_review(client)['completed']
    assert reviewed.read_bytes() == old_bytes
    assert client.get(BASE + '/plan').json()['segments'][0]['video']['path'] == str(current)
    versions = client.get(BASE + '/segments/S01/versions').json()
    assert len(versions['items']) == 2
    assert len(versions['review_records']) == (4 if stage == 'finish' else 2)


def test_same_path_new_task_invalidates_but_duplicate_receipt_does_not(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    video = complete_candidate(client, project, 'first')
    assert approve(client, video).status_code == 200
    records = backend.project_records('preset-test')
    task = backend.get_task('first')
    _, plan_path = backend.load_project_plan('preset-test')
    backend.record_segment_result('S01', 'first', 'frozen-fixture.json', task['result'], plan_path,
                                  task_id='first', payload=task['payload'], generated_at=100)
    assert state(client)['reviews']['S01']['sample']['status'] == 'approved'
    assert backend.project_records('preset-test') == records
    complete_candidate(client, project, 'second', path=video, when=200)
    after = state(client)
    assert after['reviews']['S01']['sample']['status'] == 'stale'
    assert after['readiness']['segments'][0]['state'] == 'review_stale'
    assert approve(client, video).status_code == 200
    assert state(client)['reviews']['S01']['sample']['status'] == 'approved'


@pytest.mark.parametrize('same_path', [False, True])
def test_old_page_cannot_approve_replacement_and_writes_no_record(preset_client, same_path):
    client, project = preset_client
    add_shot(client, project, 'S01')
    first = complete_candidate(client, project, 'first')
    old_revision = client.get(BASE + '/plan').json()['revision']
    complete_candidate(client, project, 'second', path=first if same_path else None, when=200)
    records = backend.project_records('preset-test')
    response = approve(client, first, expected_plan_revision=old_revision)
    assert response.status_code == 409, response.text
    assert backend.project_records('preset-test') == records
    if not same_path:
        # Even a current plan revision cannot approve a hidden older file.
        response = approve(client, first)
        assert response.status_code == 409, response.text
        assert backend.project_records('preset-test') == records


@pytest.mark.parametrize('reference', [None, 'matching'])
def test_unbound_legacy_approval_is_conservatively_stale_without_rewriting_history(preset_client, reference):
    client, project = preset_client
    add_shot(client, project, 'S01')
    video = complete_candidate(client, project, 'first')
    backend.append_project_record('review:sample', {
        'stage': 'sample', 'status': 'approved', 'note': 'retained legacy opinion',
        'candidate_ref': str(video) if reference else None,
    }, asset_id='S01', project_id='preset-test')
    records = backend.project_records('preset-test')
    after = state(client)
    assert after['reviews']['S01']['sample']['status'] == 'stale'
    assert after['readiness']['segments'][0]['state'] == 'review_stale'
    assert not guide_review(client)['completed']
    assert backend.project_records('preset-test') == records
    assert after['history'][-1]['data']['status'] == 'approved'


@pytest.mark.parametrize('missing', ['expected_plan_revision', 'candidate_ref'])
def test_approval_without_client_candidate_identity_remains_unbound(preset_client, missing):
    client, project = preset_client
    add_shot(client, project, 'S01')
    video = complete_candidate(client, project, 'first')
    response = approve(client, video, **{missing: None})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'stale'
    after = state(client)
    assert after['reviews']['S01']['sample']['status'] == 'stale'
    assert after['history'][-1]['data']['status'] == 'stale'
    assert after['history'][-1]['data']['requested_status'] == 'approved'
    # A fresh explicit approval can bind the now-visible task and become usable.
    assert approve(client, video).status_code == 200
    assert state(client)['reviews']['S01']['sample']['status'] == 'approved'


@pytest.mark.parametrize('revision', [True, 1.0, -1])
def test_invalid_plan_revision_is_rejected_without_review(preset_client, revision):
    client, project = preset_client
    add_shot(client, project, 'S01')
    video = complete_candidate(client, project, 'first')
    records = backend.project_records('preset-test')
    assert approve(client, video, expected_plan_revision=revision).status_code == 422
    assert backend.project_records('preset-test') == records


def test_visible_alternate_identity_changes_only_its_bound_review(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    primary = complete_candidate(client, project, 'first')
    assert approve(client, primary).status_code == 200
    document, plan_path = backend.load_project_plan('preset-test')
    alternate = primary.with_name('finish.mp4')
    alternate.write_bytes(b'isolated finish')
    document['segments'][0]['video']['finish_review'] = {'path': str(alternate), 'prompt_id': 'finish-first'}
    backend.write_plan_version(plan_path, document)
    assert state(client)['reviews']['S01']['sample']['status'] == 'approved'
    assert approve(client, alternate, 'finish').status_code == 200
    records = backend.project_records('preset-test')
    document, _ = backend.load_project_plan('preset-test')
    document['segments'][0]['video']['finish_review']['prompt_id'] = 'finish-next'
    backend.write_plan_version(plan_path, document)
    after = state(client)
    assert after['reviews']['S01']['sample']['status'] == 'approved'
    assert after['reviews']['S01']['finish']['status'] == 'stale'
    assert after['readiness']['segments'][0]['state'] == 'review_stale'
    assert backend.project_records('preset-test') == records


def test_receipt_invalidation_uses_frozen_private_owner_not_another_account(private_workbench):
    owner, other, ownership, _ = private_workbench
    first, second = create(owner), create(other)
    fixtures = []
    for client, project, username in ((owner, first, 'user001'), (other, second, 'user002')):
        root = '/api/projects/' + project['id']
        assert client.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5}).status_code == 200
        workspace = UserContext(username, ownership.layout).project(project['id'])
        plan_path = workspace / 'plans/plan.json'
        document = json.loads(plan_path.read_text(encoding='utf-8'))
        video = workspace / 'assets/current.mp4'
        video.write_bytes(b'isolated private current')
        document['segments'][0].update(status='video_generated', video={'path': str(video)},
                                       current_version_task_id='initial-' + username,
                                       comfyui_task_id='initial-' + username)
        backend.write_plan_version(plan_path, document)
        response = client.post(root + '/reviews', json={
            'asset_id': 'S01', 'stage': 'sample', 'status': 'approved', 'candidate_ref': str(video),
            'expected_revision': 0, 'expected_plan_revision': document['revision'], 'note': 'isolated owner selection',
        })
        assert response.status_code == 200, response.text
        fixtures.append((root, workspace, plan_path))
    other_records = backend.project_records(second['id'])
    root, workspace, plan_path = fixtures[0]
    replacement = workspace / 'assets/replacement.mp4'
    replacement.write_bytes(b'isolated replacement')
    # The production receipt handler runs with no browser/login principal.
    backend.record_segment_result('S01', 'next-owner-task', 'frozen.json', {
        'status': 'success', 'outputs': [replacement.name], 'published_outputs': [str(replacement)],
    }, plan_path, task_id='next-owner-task', payload={'project_id': first['id'], 'owner_user': 'user001'})
    assert owner.get(root + '/state').json()['reviews']['S01']['sample']['status'] == 'stale'
    assert other.get(fixtures[1][0] + '/state').json()['reviews']['S01']['sample']['status'] == 'approved'
    assert backend.project_records(second['id']) == other_records
    assert other.get(root + '/state').status_code == 404
    assert other.post(root + '/reviews', json={'asset_id': 'S01', 'stage': 'sample', 'status': 'approved',
                                            'expected_revision': 0, 'expected_plan_revision': 0}).status_code == 404
    assert other.get('/media-file', params={'path': str(replacement)}).status_code == 403


def test_mcp_explicit_adoption_binds_current_identity_and_rejects_old_same_path_page(preset_client):
    import asyncio
    import httpx
    pytest.importorskip('mcp')
    from mcp import Client
    from mcp_server.server import create_server

    client, project = preset_client
    add_shot(client, project, 'S01')
    video = complete_candidate(client, project, 'first')
    old_revision = client.get(BASE + '/plan').json()['revision']
    complete_candidate(client, project, 'second', path=video, when=200)
    records = backend.project_records('preset-test')
    args = {'project_id': 'preset-test', 'asset_id': 'S01', 'stage': 'sample', 'candidate_ref': str(video),
            'expected_plan_revision': old_revision, 'expected_review_revision': 0,
            'note': 'Isolated explicit agent selection', 'confirm_adoption': True}

    async def verify():
        async with Client(create_server('http://localhost', httpx.ASGITransport(app=backend.app))) as agent:
            rejected = (await agent.call_tool('approve_candidate', args)).structured_content
            assert not rejected['ok'] and rejected['error']['http_status'] == 409
            assert backend.project_records('preset-test') == records
            args['expected_plan_revision'] = client.get(BASE + '/plan').json()['revision']
            accepted = (await agent.call_tool('approve_candidate', args)).structured_content
            assert accepted['ok'] and accepted['data']['status'] == 'approved'
    asyncio.run(verify())
    assert state(client)['reviews']['S01']['sample']['status'] == 'approved'


def test_new_segment_result_stales_adjacent_join_and_final_review(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    left = complete_candidate(client, project, 'first')
    add_shot(client, project, 'S02')
    document, plan_path = backend.load_project_plan('preset-test')
    right = project / 'workspaces/preset-test/assets/right.mp4'
    master = project / 'workspaces/preset-test/assets/master.mp4'
    right.write_bytes(b'isolated right')
    master.write_bytes(b'isolated old master')
    document['segments'][1].update(video={'path': str(right)}, current_version_task_id='right-task')
    document['assembly'] = {'output': str(master), 'prompt_id': 'master-task', 'status': 'candidate_generated'}
    backend.write_plan_version(plan_path, document)
    response = client.put(BASE + '/assembly/joins/S01/S02', json={
        'expected_revision': document['revision'], 'status': 'approved', 'note': 'isolated join approval',
    })
    assert response.status_code == 200, response.text
    # Saving a join marks old masters stale; set up a fresh master receipt fixture.
    document, _ = backend.load_project_plan('preset-test')
    document['assembly']['status'] = 'candidate_generated'
    backend.write_plan_version(plan_path, document)
    backend.persist_review(backend.ReviewRecordRequest(
        asset_id='MASTER', stage='final', status='approved', candidate_ref=str(master), note='old master approval',
    ), 'preset-test')
    assert approve(client, left).status_code == 200
    before = state(client)
    assert before['reviews']['MASTER']['final']['status'] == 'approved'
    complete_candidate(client, project, 'second', when=200)
    after = state(client)
    assert after['reviews']['MASTER']['final']['status'] == 'stale'
    assert after['reviews']['MASTER']['final']['candidate_ref'] == str(master)
    edit = client.get(BASE + '/assembly/edit').json()
    assert edit['joins'][0]['status'] == 'stale'
    assert edit['blocking_joins'] == ['S01>S02']
    assert client.get(BASE + '/plan').json()['assembly']['status'] == 'stale'
    guide = client.get('/api/settings/guide', params={'project_id': 'preset-test'}).json()
    assert not next(row for row in guide['steps'] if row['id'] == 'assembly')['completed']
    assert master.read_bytes() == b'isolated old master'


def test_assembly_freezes_selected_current_version_without_reviving_old_approval(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    first = complete_candidate(client, project, 'first')
    second = complete_candidate(client, project, 'second', when=200)
    assert approve(client, second).status_code == 200
    revision = client.get(BASE + '/plan').json()['revision']
    assert client.post(BASE + '/segments/S01/versions/first/restore',
                       json={'expected_plan_revision': revision}).status_code == 200
    builder = project / 'tools/current_builder.py'
    builder.parent.mkdir(exist_ok=True)
    builder.write_text("def build_graph(plan, audio, prefix):\n    return {'video': plan['segments'][0]['video']['path']}\n", encoding='utf-8')
    manifest = backend.load_project_manifest('preset-test')
    manifest['assembly'] = {'builder': 'tools/current_builder.py'}
    backend.save_project_manifest(manifest)
    graph = json.loads(backend.build_assembly_snapshot(manifest).read_text(encoding='utf-8'))
    assert graph['video'] == str(first)
    assert state(client)['reviews']['S01']['sample']['status'] == 'stale'
    assert first.is_file() and second.is_file()
