"""Join-guide completion against actual services; fixtures never use a GPU."""
import asyncio
import json

import httpx
import pytest
from mcp import Client

from backend import app as backend
from mcp_server.server import create_server
from test_assembly_edit import edit_client
from test_private_workbench import private_workbench, create


@pytest.mark.parametrize(('case', 'expected_state', 'completed'), [
    ('no_videos', 'video_required', False),
    ('left_only', 'video_required', False),
    ('pending', 'pending', False),
    ('approved', 'approved', True),
    ('redo', 'redo_left', False),
    ('stale', 'stale', False),
    ('missing_file', 'video_required', False),
    ('single', 'not_applicable', False),
])
def test_join_guide_uses_version_bound_review_service_http_mcp(
        edit_client, case, expected_state, completed):
    client, root, workspace, plan_path = edit_client
    document, _ = backend.load_project_plan('edit-test')
    # Readable synthetic candidates are supplied only to the explicit review cases.
    # Missing-video cases leave metadata empty and reject approval; no fake success.
    if case == 'no_videos':
        for row in document['segments']:
            row.pop('video', None)
            row.pop('current_version_task_id', None)
    elif case == 'left_only':
        document['segments'][1].pop('video', None)
        document['segments'][1].pop('current_version_task_id', None)
    elif case == 'missing_file':
        document['segments'][1]['video']['path'] = str(workspace / 'not-created.mp4')
    elif case == 'single':
        document['segments'] = document['segments'][:1]
    backend.write_plan_version(plan_path, document)
    if case in {'approved', 'redo', 'stale'}:
        reviewed = client.put(root + '/assembly/joins/S01/S02', json={
            'expected_revision': document['revision'], 'status': 'redo_left' if case == 'redo' else 'approved',
            'note': 'Explicit isolated review; not a quality acceptance',
        })
        assert reviewed.status_code == 200, reviewed.text
        if case == 'stale':
            document, _ = backend.load_project_plan('edit-test')
            document['segments'][1]['current_version_task_id'] = 'new-fixture-version'
            backend.write_plan_version(plan_path, document)
    before_plan = client.get(root + '/plan').json()
    before_bytes = plan_path.read_bytes()
    before_state = client.get(root + '/state').json()
    before_tasks = client.get(root + '/tasks').json()
    edit = client.get(root + '/assembly/edit').json()
    preflight = client.get(root + '/assembly/preflight').json()
    data = client.get('/api/settings/guide', params={'project_id': 'edit-test'}).json()
    step = next(row for row in data['steps'] if row['id'] == 'joins')
    assert step['completed'] is completed
    assert step['applicable'] is (case != 'single')
    assert step['state'] == expected_state
    assert step['state_label']
    assert edit['join_review'] == preflight['join_review']
    assert edit['join_review']['completed'] is completed
    assert data['readiness']['assembly']['preflight']['join_review'] == edit['join_review']
    if case in {'no_videos', 'left_only', 'missing_file'}:
        assert edit['ready'] is False
        assert not edit['joins'][0]['review_ready']
        refused = client.put(root + '/assembly/joins/S01/S02', json={
            'expected_revision': before_plan['revision'], 'status': 'approved',
        })
        assert refused.status_code == 422
    if case == 'pending':
        assert edit['blocking_joins'] == []  # Preserve old unconfigured assembly compatibility.
        assert edit['ready'] is True  # Assembly eligibility does not prove reviewed joins.
        assert step['completed'] is False

    async def verify():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app))
        async with Client(server) as agent:
            for name, args, expected in (
                ('read_assembly_edit', {'project_id': 'edit-test'}, edit),
                ('preflight_assembly_edit', {'project_id': 'edit-test'}, preflight),
                ('read_guide_settings', {'project_id': 'edit-test'}, data),
            ):
                result = (await agent.call_tool(name, args)).structured_content
                assert result['ok'] and result['data'] == expected
    asyncio.run(verify())
    assert client.get(root + '/plan').json() == before_plan
    assert plan_path.read_bytes() == before_bytes
    assert client.get(root + '/state').json() == before_state
    assert client.get(root + '/tasks').json() == before_tasks
    (workspace / 'guide-joins-browser.json').write_text(
        json.dumps({'case': case, 'guide': data, 'edit': edit}, ensure_ascii=False), encoding='utf-8')


def test_learning_join_step_does_not_review_missing_candidates(edit_client):
    client, root, _, plan_path = edit_client
    document, _ = backend.load_project_plan('edit-test')
    for row in document['segments']:
        row.pop('video', None)
        row.pop('current_version_task_id', None)
    backend.write_plan_version(plan_path, document)
    before = plan_path.read_bytes()
    response = client.patch('/api/settings/guide', json={
        'expected_revision': 0, 'project_id': 'edit-test', 'completed_steps': ['joins'],
    })
    assert response.status_code == 200, response.text
    step = next(row for row in response.json()['steps'] if row['id'] == 'joins')
    assert not step['completed']
    assert step['state'] == 'video_required'
    assert plan_path.read_bytes() == before
    assert client.get(root + '/assembly/edit').json()['joins'][0]['status'] == 'pending'


def test_private_missing_join_candidates_are_unfinished_and_account_scoped(private_workbench):
    a, b, _, _ = private_workbench
    project = create(a)
    root = '/api/projects/' + project['id']
    for shot_id, duration in [('S01', 7.3), ('S02', 4)]:
        result = a.post(root + '/plan/segments', json={'segment_id': shot_id, 'duration_seconds': duration})
        assert result.status_code == 200, result.text
    before = a.get(root + '/state').json()
    edit = a.get(root + '/assembly/edit').json()
    data = a.get('/api/settings/guide', params={'project_id': project['id']}).json()
    assert edit['joins'][0]['status'] == 'pending'
    assert edit['joins'][0]['signature']['left_video'] is None
    assert edit['joins'][0]['signature']['right_video'] is None
    assert edit['blocking_joins'] == []
    assert not edit['ready'] and not edit['join_review']['completed']
    assert not next(row for row in data['steps'] if row['id'] == 'joins')['completed']
    assert b.get(root + '/assembly/edit').status_code == 404
    assert b.get('/api/settings/guide', params={'project_id': project['id']}).status_code == 404

    async def verify():
        server = create_server('http://localhost', httpx.ASGITransport(app=backend.app),
                               session_token=a.headers['Authorization'][7:])
        async with Client(server) as agent:
            for name, expected in [('read_assembly_edit', edit), ('read_guide_settings', data)]:
                result = (await agent.call_tool(name, {'project_id': project['id']})).structured_content
                assert result['ok'] and result['data'] == expected
    asyncio.run(verify())
    assert a.get(root + '/state').json() == before
    assert a.get(root + '/tasks').json()['tasks'] == []
