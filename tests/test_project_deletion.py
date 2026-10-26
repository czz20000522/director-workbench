"""Two-phase project deletion: the preview freezes the scope, apply refuses drift."""
import json
import shutil
import time
from pathlib import Path

import pytest

from backend import app as backend
from backend.routers import project_deletion


@pytest.fixture
def deletion_client(preset_client_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(backend, 'RUNTIME', tmp_path / 'runtime')
    client, project = preset_client_factory()
    return client, project


def _create(client, project_id: str, title: str) -> dict:
    response = client.post('/api/projects/create', json={
        'creation_mode': 'advanced', 'series': '测试', 'title': title,
        'project_id': project_id, 'select': False,
    })
    assert response.status_code == 200, response.text
    return response.json()['project']


def _insert_task(project_id: str, task_id: str, workflow: Path | None = None) -> None:
    payload = {'workflow': str(workflow)} if workflow is not None else {}
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
            (task_id, 'S01', 'succeeded', time.time(), time.time(), json.dumps(payload), project_id),
        )
        connection.commit()


def _task_ids(project_id: str) -> list[str]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT id FROM tasks WHERE project_id = ?', (project_id,)).fetchall()
    return [row[0] for row in rows]


def _ids(client) -> list[str]:
    return [item['id'] for item in client.get('/api/projects').json()['projects']]


def test_dossier_reports_workspace_files_without_touching_them(deletion_client):
    client, project = deletion_client
    work = _create(client, 'dossier', '档案')
    workspace = project / 'workspaces' / 'dossier'
    (workspace / 'assets').mkdir(parents=True, exist_ok=True)
    (workspace / 'assets' / 'a.txt').write_text('hello', encoding='utf-8')
    body = client.get('/api/projects/dossier/dossier').json()
    assert body['project_id'] == 'dossier' and body['title'] == '档案'
    assert body['file_count'] >= 1 and body['size_bytes'] >= 5
    assert any(item['path'] == 'assets/a.txt' for item in body['files'])
    assert Path(body['workspace']).name == 'dossier'
    assert client.get('/api/projects/missing/dossier').status_code == 404


def test_apply_deletes_previewed_scope_and_leaves_the_rest(deletion_client):
    client, project = deletion_client
    _create(client, 'keep', '保留')
    _create(client, 'drop', '删除')
    snapshot = backend.SNAPSHOT_ROOT / 'api-drop.json'
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text('{"1":{}}', encoding='utf-8')
    _insert_task('drop', 'task-drop', snapshot)
    _insert_task('keep', 'task-keep')
    workspace = project / 'workspaces/drop'

    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['drop']})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    entry = body['entries'][0]
    assert entry['project_id'] == 'drop'
    assert entry['workspace'] == str(workspace.resolve())
    assert entry['workspace_exists'] is True
    assert entry['snapshot']['file_count'] >= 1
    assert 'director.project.json' in entry['snapshot']['sample']
    assert '不会被删除' in body['preserved']
    assert workspace.is_dir()

    refused = client.post('/api/projects/deletion', json={
        'plan_id': body['plan_id'], 'project_ids': ['drop'],
    })
    assert refused.status_code == 422
    assert refused.json()['detail']['code'] == 'confirmation_required'
    assert workspace.is_dir()
    assert 'drop' in _ids(client)

    applied = client.post('/api/projects/deletion', json={
        'plan_id': body['plan_id'], 'project_ids': ['drop'], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert applied.json()['deleted'] == ['drop']
    assert not workspace.exists()
    assert not (backend.PROJECTS_ROOT / 'drop.json').exists()
    assert not snapshot.exists()
    assert _task_ids('drop') == []
    assert _task_ids('keep') == ['task-keep']
    assert _ids(client) == ['preset-test', 'keep']
    assert (project / 'workspaces/keep').is_dir()
    assert not list((backend.RUNTIME / 'deletion-plans').glob('*.json'))


def test_apply_rejects_workspace_drift(deletion_client):
    client, project = deletion_client
    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']}).json()
    (project / 'workspaces/preset-test/late-file.txt').write_text('新增', encoding='utf-8')

    response = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': ['preset-test'], 'confirm': True,
    })
    assert response.status_code == 409
    assert '重新预览' in response.json()['detail']
    assert (project / 'workspaces/preset-test').is_dir()
    assert (backend.PROJECTS_ROOT / 'preset-test.json').is_file()


def test_apply_rejects_a_wider_scope_than_previewed(deletion_client):
    client, _ = deletion_client
    _create(client, 'extra', '额外')
    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']}).json()

    response = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': ['preset-test', 'extra'], 'confirm': True,
    })
    assert response.status_code == 409
    assert 'extra' in _ids(client)


def test_missing_workspace_is_reported_and_still_retires_the_registration(deletion_client):
    client, project = deletion_client
    stale = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']}).json()
    shutil.rmtree(project / 'workspaces/preset-test')

    drifted = client.post('/api/projects/deletion', json={
        'plan_id': stale['plan_id'], 'project_ids': ['preset-test'], 'confirm': True,
    })
    assert drifted.status_code == 409

    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']})
    assert plan.status_code == 200, plan.text
    entry = plan.json()['entries'][0]
    assert entry['workspace_exists'] is False
    assert entry['snapshot']['file_count'] == 0

    applied = client.post('/api/projects/deletion', json={
        'plan_id': plan.json()['plan_id'], 'project_ids': ['preset-test'], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert not (backend.PROJECTS_ROOT / 'preset-test.json').exists()
    assert _ids(client) == []


def test_deleting_the_active_project_repoints_the_selection(deletion_client):
    client, _ = deletion_client
    _create(client, 'second', '第二部')
    assert client.get('/api/project').json()['id'] == 'preset-test'
    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']}).json()

    applied = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': ['preset-test'], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert backend.load_project_catalog()['default_project_id'] == 'second'
    assert client.get('/api/project').json()['id'] == 'second'


def test_expired_plan_requires_a_new_preview(deletion_client):
    client, _ = deletion_client
    plan = client.post('/api/projects/deletion-plan', json={'project_ids': ['preset-test']}).json()
    path = backend.RUNTIME / 'deletion-plans' / f"{plan['plan_id']}.json"
    document = json.loads(path.read_text(encoding='utf-8'))
    document['created_at'] = time.time() - project_deletion.PLAN_TTL_SECONDS - 1
    path.write_text(json.dumps(document), encoding='utf-8')

    response = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': ['preset-test'], 'confirm': True,
    })
    assert response.status_code == 409
    assert '过期' in response.json()['detail']
    assert not path.exists()


def test_series_folder_is_pruned_once_its_last_project_goes(deletion_client):
    _, project = deletion_client
    series = project / 'workspaces/系列'
    (series / '作品').mkdir(parents=True)
    shutil.rmtree(series / '作品')
    project_deletion._prune_series_directory(series)

    assert not series.exists()
    assert (project / 'workspaces').is_dir()


def test_unknown_plan_and_bad_ids_are_rejected(deletion_client):
    client, _ = deletion_client
    assert client.post('/api/projects/deletion', json={
        'plan_id': '0' * 24, 'project_ids': ['preset-test'], 'confirm': True,
    }).status_code == 404
    assert client.post('/api/projects/deletion', json={
        'plan_id': '../escape', 'project_ids': ['preset-test'], 'confirm': True,
    }).status_code == 422
    missing = client.post('/api/projects/deletion-plan', json={'project_ids': ['nope']})
    assert missing.status_code == 404
