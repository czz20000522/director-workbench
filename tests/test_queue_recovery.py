"""Ownerless ``needs_reconcile`` recovery: read-only plan and explicit dispositions.

The environment is fully isolated: private mode, a synthetic ownership registry,
in-process SQLite rows and no GPU, scheduler tick or network access.  The tests
mount only ``queue_recovery.ROUTER`` because the Lead owns app.py registration
(and the private-mode allow-list) of new routers.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend import queue_recovery, task_scheduler
from backend.private_auth import Principal, current_principal
from backend.routers.queue_recovery import ROUTER
from backend.user_context import SessionIdentity
from support import create

ADMIN = 'user000'
OWNER = 'user001'
OTHER = 'user002'
PLAN = '/api/admin/tasks/recovery-plan'


def repair_path(task_id: str) -> str:
    return f'/api/admin/tasks/{task_id}/repair-owner'


def resolve_path(project_id: str, task_id: str) -> str:
    return f'/api/projects/{project_id}/tasks/{task_id}/resolve-ownerless'


def _principal(username: str) -> Principal | None:
    if not username:
        return None
    return Principal(SessionIdentity(username, time.time() + 300), 'test', username == ADMIN)


@pytest.fixture
def recovery_env(private_workbench):
    owner_client, other_client, ownership, _root = private_workbench
    project = create(owner_client)
    other_project = create(other_client)
    api = FastAPI()
    api.include_router(ROUTER)

    @api.middleware('http')
    async def identity_middleware(request, call_next):
        # Private mode normally sets this context variable before the route; the
        # router-only app reproduces that boundary without duplicating its path
        # allow-list (the Lead wires the real mount and allow-list).
        token = current_principal.set(_principal(request.headers.get('x-test-user', ''))) \
            if request.headers.get('x-test-user') else None
        try:
            return await call_next(request)
        finally:
            if token is not None:
                current_principal.reset(token)

    return {
        'client': TestClient(api),
        'api': api,
        'project': project,
        'other_project': other_project,
        'ownership': ownership,
        'owner_client': owner_client,
        'other_client': other_client,
    }


def insert_task(project_id: str, task_id: str, *, status: str = 'needs_reconcile',
                payload: dict | None = None, prompt_id: str | None = None,
                result: dict | None = None) -> str:
    body = {'asset_id': 'S01', 'project_id': project_id, 'kind': 'video', **(payload or {})}
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,prompt_id,created_at,updated_at,stop_requested,payload,result,project_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (task_id, 'S01', status, prompt_id, now, now, 0,
             json.dumps(body, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result is not None else None, project_id))
        connection.commit()
    return task_id


def raw_task(task_id: str) -> dict:
    with backend.DB_LOCK, backend.db() as connection:
        row = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    assert row is not None
    return backend.task_row(row)


def needs_reconcile_rows() -> int:
    with backend.DB_LOCK, backend.db() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='needs_reconcile'").fetchone()[0])


def occupied_gpu_task_ids() -> set[str]:
    """Mirror the scheduler's occupied-slot rule without ticking it."""
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks').fetchall()
    occupied = set()
    for row in rows:
        task = backend.task_row(row)
        if task_scheduler.resource(task) == 'gpu' and task['status'] not in task_scheduler.WAITING | task_scheduler.TERMINAL:
            occupied.add(task['id'])
    return occupied


def admin_headers(administrator: bool = True) -> dict[str, str]:
    return {'x-test-user': ADMIN if administrator else OWNER}


def test_plan_is_read_only_and_reports_unique_owner(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    task_id = insert_task(project['id'], 'stuck-1')
    before = raw_task(task_id)
    assert task_id in occupied_gpu_task_ids()

    response = client.get(PLAN, headers=admin_headers())
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['read_only'] is True
    assert report['private_workspaces'] is True
    assert report['counts']['owner_repairable'] == 1
    entry = report['tasks'][0]
    assert entry['task_id'] == task_id
    assert entry['recovery_state'] == 'owner_repairable'
    assert entry['inferred_owner'] == OWNER
    assert entry['occupies_execution_slot'] is True
    assert {action['id'] for action in entry['actions']} == {'repair_owner', 'resolve_ownerless'}
    assert entry['blockers'] == []

    # The plan is read-only: same facts on a second read, identical frozen row.
    again = client.get(PLAN, headers=admin_headers()).json()
    assert again['tasks'] == report['tasks'] and again['excluded'] == report['excluded']
    after = raw_task(task_id)
    assert after['payload'] == before['payload']
    assert after['status'] == before['status']
    assert after['updated_at'] == before['updated_at']
    assert task_id in occupied_gpu_task_ids()

    # Local (no private-mode principal) operators can read the plan too.
    assert client.get(PLAN).status_code == 200
    # Ordinary project owners cannot use the cross-project administration view.
    denied = client.get(PLAN, headers=admin_headers(False))
    assert denied.status_code == 403
    assert denied.json()['detail']['code'] == 'admin_required'
    assert client.get(PLAN).status_code == 200


def test_repair_owner_then_user_resolution_is_idempotent_and_never_succeeds(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    task_id = insert_task(project['id'], 'stuck-2')
    assert task_id in occupied_gpu_task_ids()
    json_body = {'confirm': True, 'note': 'checked project registry'}

    repaired = client.post(repair_path(task_id), json=json_body, headers=admin_headers())
    assert repaired.status_code == 200, repaired.text
    body = repaired.json()
    assert body['action'] == 'owner_repaired'
    assert body['idempotent'] is False
    assert body['owner_user'] == OWNER
    assert body['status'] == 'needs_reconcile'
    # Repairing the owner does not release the slot by itself.
    assert body['occupies_execution_slot'] is True
    assert task_id in occupied_gpu_task_ids()
    stored = raw_task(task_id)
    assert stored['status'] == 'needs_reconcile'
    assert stored['payload']['owner_user'] == OWNER
    assert stored['payload']['owner_recovery']['state'] == 'owner_repaired'
    assert len(stored['payload']['owner_recovery']['history']) == 1
    assert stored['payload']['owner_recovery']['history'][0]['action'] == 'owner_repaired'
    assert stored['payload']['owner_recovery']['history'][0]['actor'] == {'kind': 'admin', 'username': ADMIN}

    # Same request again: no duplicate audit entry, same disposition.
    repeated = client.post(repair_path(task_id), json=json_body, headers=admin_headers())
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['idempotent'] is True
    assert repeated.json()['action'] == 'owner_repaired'
    assert len(raw_task(task_id)['payload']['owner_recovery']['history']) == 1

    plan = client.get(PLAN, headers=admin_headers()).json()
    assert plan['counts']['owner_repaired'] == 1
    repaired_entry = next(row for row in plan['tasks'] if row['task_id'] == task_id)
    assert repaired_entry['recovery_state'] == 'owner_repaired'
    assert 'resolve_ownerless' in {action['id'] for action in repaired_entry['actions']}

    resolution_body = {'confirm_not_submitted': True, 'note': 'payload has no prompt_id or receipt'}
    resolved = client.post(resolve_path(project['id'], task_id), json=resolution_body,
                           headers={'x-test-user': OWNER})
    assert resolved.status_code == 200, resolved.text
    result = resolved.json()
    assert result['action'] == 'resolved_not_submitted'
    assert result['status'] == 'stopped'
    assert result['occupies_execution_slot'] is False
    stored = raw_task(task_id)
    assert stored['status'] == 'stopped'
    assert stored['status'] != 'succeeded'
    assert stored['payload']['owner_recovery']['state'] == 'resolved_not_submitted'
    assert len(stored['payload']['owner_recovery']['history']) == 2
    assert stored['result']['ownerless_recovery']['action'] == 'resolved_not_submitted'
    assert '未视为成功' in stored['error']
    assert needs_reconcile_rows() == 0
    assert task_id not in occupied_gpu_task_ids()

    # Repeating the resolution is idempotent and keeps the same audit tail.
    repeated_resolution = client.post(resolve_path(project['id'], task_id), json=resolution_body,
                                      headers={'x-test-user': OWNER})
    assert repeated_resolution.status_code == 200, repeated_resolution.text
    assert repeated_resolution.json()['idempotent'] is True
    assert len(raw_task(task_id)['payload']['owner_recovery']['history']) == 2

    # A terminal resolution cannot be re-opened as an owner repair.
    refused = client.post(repair_path(task_id), json=json_body, headers=admin_headers())
    assert refused.status_code == 409
    assert refused.json()['detail']['code'] == 'already_resolved_not_submitted'


def test_owner_conflict_marks_needs_operator_and_releases_slot(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    other_project = recovery_env['other_project']
    # The frozen task identity points at two registered projects with different owners.
    task_id = insert_task(project['id'], 'stuck-conflict', payload={'project_id': other_project['id']})
    assert task_id in occupied_gpu_task_ids()

    report = client.get(PLAN, headers=admin_headers()).json()
    entry = next(row for row in report['tasks'] if row['task_id'] == task_id)
    assert entry['recovery_state'] == 'owner_conflict'
    assert entry['inferred_owner'] is None
    assert entry['occupies_execution_slot'] is True
    assert [blocker['code'] for blocker in entry['blockers']] == ['project_identity_conflict']

    body = {'confirm': True, 'note': 'two project identities disagree'}
    marked = client.post(repair_path(task_id), json=body, headers=admin_headers())
    assert marked.status_code == 200, marked.text
    result = marked.json()
    assert result['action'] == 'needs_operator'
    assert result['status'] == 'stopped'
    assert result['owner_user'] is None
    assert [blocker['code'] for blocker in result['blockers']] == ['project_identity_conflict']
    stored = raw_task(task_id)
    assert stored['status'] == 'stopped' and stored['status'] != 'succeeded'
    assert not stored['payload'].get('owner_user')
    assert stored['payload']['owner_recovery']['state'] == 'needs_operator'
    assert stored['result']['ownerless_recovery']['action'] == 'needs_operator'
    assert needs_reconcile_rows() == 0
    assert task_id not in occupied_gpu_task_ids()

    repeated = client.post(repair_path(task_id), json=body, headers=admin_headers())
    assert repeated.status_code == 200 and repeated.json()['idempotent'] is True
    assert len(raw_task(task_id)['payload']['owner_recovery']['history']) == 1


def test_startup_sweep_releases_only_pre_submission_ownerless_rows(recovery_env):
    project = recovery_env['project']
    owner_task = insert_task(project['id'], 'sweep-owner')
    unknown_task = insert_task('not-registered-project', 'sweep-unknown')
    submitted_task = insert_task(project['id'], 'sweep-submitted', prompt_id='p3')
    assert {owner_task, unknown_task, submitted_task} <= occupied_gpu_task_ids()

    result = queue_recovery.startup_sweep(backend)
    outcomes = {row['task_id']: row['outcome'] for row in result['handled']}
    assert outcomes == {owner_task: 'resolved_not_submitted', unknown_task: 'needs_operator'}

    repaired = raw_task(owner_task)
    assert repaired['status'] == 'stopped' and repaired['status'] != 'succeeded'
    assert repaired['payload']['owner_user'] == OWNER
    assert repaired['payload']['owner_recovery']['state'] == 'resolved_not_submitted'
    assert repaired['payload']['owner_recovery']['history'][-1]['actor']['kind'] == 'system'

    marked = raw_task(unknown_task)
    assert marked['status'] == 'stopped' and marked['status'] != 'succeeded'
    assert marked['payload']['owner_recovery']['state'] == 'needs_operator'
    assert not marked['payload'].get('owner_user')

    untouched = raw_task(submitted_task)
    assert untouched['status'] == 'needs_reconcile'
    assert untouched['payload'].get('owner_recovery') is None
    assert submitted_task in occupied_gpu_task_ids()
    assert owner_task not in occupied_gpu_task_ids()
    assert unknown_task not in occupied_gpu_task_ids()

    # A second startup pass has nothing actionable and writes nothing.
    history = raw_task(owner_task)['payload']['owner_recovery']['history']
    repeated = queue_recovery.startup_sweep(backend)
    assert repeated == {'schema_version': 1, 'scanned': 0, 'handled': []}
    assert raw_task(owner_task)['payload']['owner_recovery']['history'] == history


def test_submitted_rows_are_excluded_and_never_modified(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    payload_prompt = insert_task(project['id'], 'submitted-payload', payload={'prompt_id': 'p1'})
    row_prompt = insert_task(project['id'], 'submitted-row', prompt_id='p2')
    receipt = insert_task(project['id'], 'submitted-receipt',
                          payload={'submission_receipt': {'key': 'k1', 'request': {}}})
    before = {task_id: raw_task(task_id) for task_id in (payload_prompt, row_prompt, receipt)}

    report = client.get(PLAN, headers=admin_headers()).json()
    assert report['tasks'] == []
    assert report['counts']['excluded'] == 3
    for skip in report['excluded']:
        assert 'submitted_requires_receipt_reconciliation' in skip['reasons']
        assert skip['actions'] == []

    for task_id in (payload_prompt, row_prompt, receipt):
        repaired = client.post(repair_path(task_id), json={'confirm': True, 'note': 'x'},
                               headers=admin_headers())
        assert repaired.status_code == 409
        assert repaired.json()['detail']['code'] == 'submitted_requires_receipt_reconciliation'
        resolved = client.post(resolve_path(project['id'], task_id),
                               json={'confirm_not_submitted': True, 'note': 'x'},
                               headers={'x-test-user': OWNER})
        assert resolved.status_code == 409
        assert resolved.json()['detail']['code'] == 'submitted_requires_receipt_reconciliation'
        after = raw_task(task_id)
        assert after['status'] == before[task_id]['status']
        assert after['payload'] == before[task_id]['payload']


def test_resolve_requires_project_owner_confirmation_and_note(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    task_id = insert_task(project['id'], 'stuck-permission')
    before = raw_task(task_id)

    other = client.post(resolve_path(project['id'], task_id),
                        json={'confirm_not_submitted': True, 'note': 'not mine'},
                        headers={'x-test-user': OTHER})
    assert other.status_code == 404

    admin = client.post(resolve_path(project['id'], task_id),
                        json={'confirm_not_submitted': True, 'note': 'admin cannot confirm for owner'},
                        headers={'x-test-user': ADMIN})
    assert admin.status_code == 404

    unconfirmed = client.post(resolve_path(project['id'], task_id), json={'note': 'check'},
                              headers={'x-test-user': OWNER})
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()['detail']['code'] == 'confirmation_required'

    no_note = client.post(resolve_path(project['id'], task_id),
                          json={'confirm_not_submitted': True}, headers={'x-test-user': OWNER})
    assert no_note.status_code == 422
    assert no_note.json()['detail']['code'] == 'note_required'
    assert raw_task(task_id)['payload'] == before['payload']

    accepted = client.post(resolve_path(project['id'], task_id),
                           json={'confirm_not_submitted': True, 'note': 'checked frozen payload'},
                           headers={'x-test-user': OWNER})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['action'] == 'resolved_not_submitted'
    assert raw_task(task_id)['status'] == 'stopped'
    assert raw_task(task_id)['status'] != 'succeeded'


def test_repair_requires_administrator_or_local_operator(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    task_id = insert_task(project['id'], 'stuck-auth')
    body = {'confirm': True, 'note': 'x'}

    denied = client.post(repair_path(task_id), json=body, headers={'x-test-user': OWNER})
    assert denied.status_code == 403
    assert denied.json()['detail']['code'] == 'admin_required'
    assert raw_task(task_id)['payload'].get('owner_recovery') is None

    # Missing confirmation is a structured 409 before any write.
    unconfirmed = client.post(repair_path(task_id), json={'note': 'x'}, headers=admin_headers())
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()['detail']['code'] == 'confirmation_required'
    assert raw_task(task_id)['payload'].get('owner_recovery') is None

    local = client.post(repair_path(task_id), json=body)
    assert local.status_code == 200, local.text
    assert local.json()['action'] == 'owner_repaired'


def test_cpu_and_owned_rows_are_excluded_from_ownerless_set(recovery_env):
    client = recovery_env['client']
    project = recovery_env['project']
    cpu_task = insert_task(project['id'], 'cpu-stuck', payload={'kind': 'audio_edit'})
    owned_task = insert_task(project['id'], 'owned-stuck', payload={'owner_user': OWNER})

    report = client.get(PLAN, headers=admin_headers()).json()
    skipped = {row['task_id']: row['reasons'] for row in report['excluded']}
    assert 'not_gpu_resource' in skipped[cpu_task]
    assert 'owner_already_frozen' in skipped[owned_task]
    for task_id in (cpu_task, owned_task):
        response = client.post(repair_path(task_id), json={'confirm': True, 'note': 'x'},
                               headers=admin_headers())
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'task_not_ownerless_reconcilable'
    # An already owned row can still be confirmed by its project owner.
    resolved = client.post(resolve_path(project['id'], owned_task),
                           json={'confirm_not_submitted': True, 'note': 'owned by caller'},
                           headers={'x-test-user': OWNER})
    assert resolved.status_code == 200, resolved.text
    assert raw_task(owned_task)['status'] == 'stopped'


def test_local_mode_without_private_registry_marks_needs_operator(preset_client):
    client, _project = preset_client
    task_id = insert_task('preset-test', 'legacy-stuck')
    api = FastAPI()
    api.include_router(ROUTER)
    local = TestClient(api)

    report = local.get(PLAN).json()
    entry = next(row for row in report['tasks'] if row['task_id'] == task_id)
    assert entry['recovery_state'] == 'needs_operator'
    assert entry['inferred_owner'] is None
    # There is no registry owner to repair: the local operator can only mark the unknown explicitly.
    repaired = local.post(repair_path(task_id), json={'confirm': True, 'note': 'local operator'})
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()['action'] == 'needs_operator'
    assert raw_task(task_id)['status'] == 'stopped'
    assert raw_task(task_id)['status'] != 'succeeded'


def test_real_private_app_exposes_admin_routes_and_enforces_admin(private_workbench):
    """The real private-mode mount (Lead wiring) keeps the route-level gates."""
    owner_client, other_client, _ownership, _root = private_workbench
    project = create(owner_client)
    insert_task(project['id'], 'real-app-stuck')
    admin = TestClient(backend.app)
    assert admin.post('/api/auth/login', json={
        'username': ADMIN, 'password': 'admin'}).status_code == 200
    # Browser cookie writes bind the tab identity and require a same-origin request.
    admin.headers['X-Workspace-User'] = ADMIN
    admin.headers['Origin'] = 'http://testserver'

    plan = admin.get(PLAN)
    assert plan.status_code == 200, plan.text
    assert plan.json()['counts']['owner_repairable'] == 1

    # A signed-in project owner can reach the route (private mode allows the
    # path) but the route itself still requires an administrator/local operator.
    denied = owner_client.get(PLAN)
    assert denied.status_code == 403, denied.text
    assert denied.json()['detail']['code'] == 'admin_required'

    repaired = admin.post(repair_path('real-app-stuck'),
                          json={'confirm': True, 'note': 'admin registry check'})
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()['owner_user'] == OWNER

    resolved = owner_client.post(resolve_path(project['id'], 'real-app-stuck'),
                                 json={'confirm_not_submitted': True,
                                       'note': 'owner checked frozen payload'})
    assert resolved.status_code == 200, resolved.text
    assert raw_task('real-app-stuck')['status'] == 'stopped'


def test_router_paths_are_registered_for_lead_wiring(recovery_env):
    paths = recovery_env['api'].openapi()['paths']
    assert {'get'} == set(paths[PLAN])
    assert {'post'} == set(paths['/api/admin/tasks/{task_id}/repair-owner'])
    assert {'post'} == set(paths['/api/projects/{project_id}/tasks/{task_id}/resolve-ownerless'])


def test_module_rejects_untrusted_registry_and_never_invents_owner(monkeypatch, tmp_path):
    class BrokenRegistry:
        def read(self):
            raise ValueError('registry corrupt')

    monkeypatch.setattr(backend, 'PRIVATE_WORKSPACES', BrokenRegistry())
    task = {'id': 'synthetic', 'status': 'needs_reconcile', 'project_id': 'p1',
            'payload': {'asset_id': 'S01', 'project_id': 'p1', 'kind': 'video'}}
    inference = queue_recovery.infer_owner(backend, task)
    assert inference['state'] == queue_recovery.NEEDS_OPERATOR
    assert inference['owner'] is None
    assert inference['reason'] == 'ownership_registry_unavailable'
