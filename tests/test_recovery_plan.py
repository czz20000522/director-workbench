"""Read-only resume plan contract tests.

The endpoint exists so a caller can answer "where does this work stand and what
could I explicitly do next" without retrying a task, adopting a candidate or
rewriting the plan.  Every test therefore checks both the reported facts and
that the GET itself changed nothing.
"""
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import app as backend
from backend.routers import recovery as recovery_router
from backend.schemas import ReviewRecordRequest
from support import add_shot, generated_version

BASE = '/api/projects/preset-test'
RESUME = BASE + '/resume-plan'


def resume_api() -> TestClient:
    """Mount only the recovery router: the Lead owns app.py registration."""
    api = FastAPI()
    api.include_router(recovery_router.ROUTER)
    return TestClient(api)


def resume_plan() -> dict:
    response = resume_api().get(RESUME)
    assert response.status_code == 200, response.text
    return response.json()


def insert_task(asset_id: str, task_id: str, status: str) -> None:
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) '
            'VALUES (?,?,?,?,?,?,?)',
            (task_id, asset_id, status, time.time(), time.time(),
             json.dumps({'asset_id': asset_id}), 'preset-test'),
        )
        connection.commit()


def task_statuses() -> dict[str, str]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(
            'SELECT id, status FROM tasks WHERE project_id = ?', ('preset-test',)).fetchall()
    return {row['id']: row['status'] for row in rows}


def plan_revision() -> int:
    document, _ = backend.load_project_plan('preset-test')
    return int(document.get('revision', 0) or 0)


def ensure_assembly_builder(project) -> None:
    """The fixture project is isolated; give it the configured builder it names."""
    path = project / 'tools' / 'build_assembly_workflow.py'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('# isolated test builder\n', encoding='utf-8')


def segment_by_id(body: dict, asset_id: str) -> dict:
    return next(row for row in body['segments'] if row['id'] == asset_id)


def action_ids(row: dict) -> set[str]:
    return {action['id'] for action in row['next_actions']}


def blocker_codes(assembly: dict) -> set[str]:
    return {blocker.get('code') for blocker in assembly['blockers']}


def test_resume_plan_reports_generating_shot_without_retrying(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    insert_task('S01', 'task-running', 'running')
    revision = plan_revision()

    body = resume_plan()

    assert body['schema_version'] == 1
    assert body['read_only'] is True
    assert body['project_id'] == 'preset-test'
    assert body['plan']['revision'] == revision
    row = segment_by_id(body, 'S01')
    assert row['state'] == 'generating'
    assert row['task_id'] == 'task-running'
    assert row['task_status'] == 'running'
    assert {'inspect_task', 'stop_task'} <= action_ids(row)
    assert 'adopt_candidate' not in action_ids(row)
    # The GET reported the task and did not retry, stop or rewrite anything.
    assert task_statuses() == {'task-running': 'running'}
    assert plan_revision() == revision


def test_resume_plan_reports_needs_reconcile_and_offers_reconcile_only(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    insert_task('S01', 'task-lost', 'needs_reconcile')

    body = resume_plan()

    row = segment_by_id(body, 'S01')
    assert row['state'] == 'needs_reconcile'
    assert row['task_id'] == 'task-lost'
    reconcile = next(action for action in row['next_actions'] if action['id'] == 'reconcile_task')
    assert reconcile['method'] == 'POST'
    assert reconcile['path'].endswith('/tasks/task-lost/reconcile')
    assert 'submit_generation' not in action_ids(row)
    assert task_statuses() == {'task-lost': 'needs_reconcile'}


def test_resume_plan_reports_adopted_candidate_and_ready_assembly(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    video = generated_version(client, project, 'task-adopted', 'adopted prompt', time.time())
    document, _ = backend.load_project_plan('preset-test')
    backend.persist_review(ReviewRecordRequest(
        asset_id='S01', stage='sample', status='approved', note='explicit adoption',
        candidate_ref=str(video), expected_plan_revision=document['revision'],
        source='explicit-adoption'), 'preset-test')
    ensure_assembly_builder(project)
    revision = plan_revision()

    body = resume_plan()

    row = segment_by_id(body, 'S01')
    assert row['state'] == 'adopted'
    assert row['adopted'] is True
    assert row['candidate_ref'].endswith('task-adopted.mp4')
    assert row['video']['exists'] is True
    assert 'adopt_candidate' not in action_ids(row)
    assembly = body['assembly']
    assert assembly['ready'] is True, assembly['blockers']
    assert assembly['state'] == 'ready'
    assert 'assemble' in {action['id'] for action in assembly['next_actions']}
    # Reporting an adoption is not adopting again: no plan or task change.
    assert plan_revision() == revision
    assert task_statuses() == {'task-adopted': 'succeeded'}


def test_resume_plan_reports_video_generated_without_auto_adopting(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    generated_version(client, project, 'task-generated', 'preview prompt', time.time())

    body = resume_plan()

    row = segment_by_id(body, 'S01')
    assert row['state'] == 'video_generated'
    assert row['adopted'] is False
    assert row['candidate_ref'].endswith('task-generated.mp4')
    assert {'review_candidate', 'adopt_candidate'} <= action_ids(row)
    # Neither review nor adoption records were created by the read.
    state = backend.build_project_state('preset-test')
    assert state['reviews'] == {}


def test_resume_plan_lists_assembly_blockers_for_unprepared_shots(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    add_shot(client, project, 'S02')

    body = resume_plan()

    assembly = body['assembly']
    assert assembly['ready'] is False
    assert assembly['state'] == 'blocked'
    codes = blocker_codes(assembly)
    assert {'video_required', 'adoption_required'} <= codes
    dependencies = {blocker.get('dependency') for blocker in assembly['blockers']}
    assert {'S01', 'S02'} <= dependencies
    adopt = next(blocker for blocker in assembly['blockers']
                 if blocker.get('code') == 'adoption_required')
    assert adopt['action']['id'] == 'adopt_candidate'
    assert 'assemble' not in {action['id'] for action in body['next_explicit_actions']}


def test_resume_plan_reports_an_adopted_assembly_candidate(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    document, plan_path = backend.load_project_plan('preset-test')
    master = project / 'workspaces/preset-test/assets/master.mp4'
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b'master candidate')
    document['assembly'] = {'status': 'candidate_generated', 'prompt_id': 'master-task',
                            'output': str(master)}
    plan_path.write_text(json.dumps(document), encoding='utf-8')
    insert_task('MASTER', 'master-task', 'succeeded')
    backend.persist_review(ReviewRecordRequest(
        asset_id='MASTER', stage='final', status='approved', note='explicit master adoption',
        candidate_ref=str(master), expected_plan_revision=document['revision'],
        source='explicit-adoption'), 'preset-test')

    body = resume_plan()

    assembly = body['assembly']
    assert assembly['adopted'] is True
    assert assembly['candidate_ref'].endswith('master.mp4')
    assert assembly['output'] == str(master)


def test_resume_plan_handles_an_empty_project(preset_client):
    _client, _project = preset_client

    body = resume_plan()

    assert body['segments'] == []
    assert body['segment_states'] == {}
    assert body['plan']['segment_count'] == 0
    assembly = body['assembly']
    assert assembly['ready'] is False
    assert assembly['state'] == 'blocked'
    assert 'segments_required' in blocker_codes(assembly)
    assert 'add_segment' in {action['id'] for action in body['next_explicit_actions']}


@pytest.mark.parametrize('status', ['failed', 'stopped'])
def test_resume_plan_reports_terminal_task_without_retry(preset_client, status):
    client, project = preset_client
    add_shot(client, project, 'S01')
    insert_task('S01', f'task-{status}', status)

    body = resume_plan()

    row = segment_by_id(body, 'S01')
    assert row['state'] == status
    assert 'submit_generation' in action_ids(row)
    assert task_statuses() == {f'task-{status}': status}


def test_resume_plan_route_rejects_unknown_project(preset_client):
    _client, _project = preset_client

    response = resume_api().get('/api/projects/does-not-exist/resume-plan')

    assert response.status_code == 404
