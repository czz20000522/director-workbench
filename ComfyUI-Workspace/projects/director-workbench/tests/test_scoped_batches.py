from backend import app as backend
from test_production_presets import preset_client, install, add_shot


def setup(client, project):
    install(client)
    add_shot(client, project, 'S01')
    add_shot(client, project, 'S02')
    assert client.post('/api/projects/create', json={'series': '测试', 'title': 'other', 'project_id': 'other'}).status_code == 200


def test_target_batch_stop_and_resume_keep_frozen_inputs(preset_client):
    client, project = preset_client
    setup(client, project)
    response = client.post('/api/projects/preset-test/batches', json={'asset_ids': ['S01', 'S02']})
    assert response.status_code == 200, response.text
    batch = response.json()
    original = [backend.get_task(task_id) for task_id in batch['task_ids']]
    assert all(task['project_id'] == 'preset-test' and 'preset-test' in task['payload']['plan_path'] for task in original)
    for action in ('stop', 'resume'):
        assert client.post(f"/api/projects/other/batches/{batch['batch_id']}/{action}").status_code == 404
    stopped = client.post(f"/api/projects/preset-test/batches/{batch['batch_id']}/stop")
    assert stopped.status_code == 200
    assert all(task['status'] == 'stopped' and task['stop_requested'] for task in stopped.json()['tasks'])
    resumed = client.post(f"/api/projects/preset-test/batches/{batch['batch_id']}/resume")
    assert resumed.status_code == 200, resumed.text
    tasks = [backend.get_task(task_id) for task_id in resumed.json()['task_ids']]
    assert [task['payload'] for task in tasks] == [task['payload'] for task in original]
    assert [task['asset_id'] for task in tasks] == ['S01', 'S02']
    assert backend.CURRENT_PROJECT_ID == 'other'


def test_stopped_waiting_batch_never_starts_runner(preset_client, monkeypatch):
    client, project = preset_client
    setup(client, project)
    batch = client.post('/api/projects/preset-test/batches', json={}).json()
    client.post(f"/api/projects/preset-test/batches/{batch['batch_id']}/stop")
    def must_not_run(*args): raise AssertionError('Stopped segment was submitted')
    monkeypatch.setattr(backend, 'run_workflow_submission', must_not_run)
    backend.run_batch(batch['batch_id'], batch['task_ids'], [backend.get_task(task_id)['payload'] for task_id in batch['task_ids']])
    assert all(backend.get_task(task_id)['status'] == 'stopped' for task_id in batch['task_ids'])


def test_duplicate_shots_and_uncertain_batch_do_not_change_tasks(preset_client):
    client, project = preset_client
    setup(client, project)
    assert client.post('/api/projects/preset-test/batches', json={'asset_ids': ['S01', 'S01']}).status_code == 422
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
    batch = client.post('/api/projects/preset-test/batches', json={}).json()
    backend.set_task(batch['task_ids'][0], status='needs_reconcile')
    before = [backend.get_task(task_id) for task_id in batch['task_ids']]
    assert client.post(f"/api/projects/preset-test/batches/{batch['batch_id']}/stop").status_code == 409
    assert [backend.get_task(task_id) for task_id in batch['task_ids']] == before
