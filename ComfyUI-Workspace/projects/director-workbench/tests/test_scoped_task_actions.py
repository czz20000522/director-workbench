import asyncio
import json
from backend import app as backend
from test_production_presets import preset_client


def prepare(client, project, monkeypatch, status='needs_reconcile'):
    assert client.post('/api/projects/preset-test/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'keep this edit'}).status_code == 200
    assert client.post('/api/projects/create', json={'series': '测试', 'title': 'other', 'project_id': 'other'}).status_code == 200
    plan = project / 'workspaces/preset-test/plans/plan.json'
    output = project / 'output'
    output.mkdir()
    (output / 'result.mp4').write_bytes(b'candidate')
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', output)
    monkeypatch.setattr(backend, 'request_json', lambda *args: {'queue_running': [], 'queue_pending': []})
    monkeypatch.setattr(backend, 'history_done', lambda *args: {'status': 'success', 'outputs': ['result.mp4']})
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    with backend.db() as connection:
        connection.execute('INSERT INTO tasks (id,asset_id,status,prompt_id,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?,?)', ('owned', 'S01', status, 'original-receipt', 1, 1, json.dumps({'project_id': 'preset-test', 'plan_path': str(plan), 'workflow': 'frozen.json', 'assembly_asset_id': ''}), 'preset-test'))
        connection.commit()
    return plan


def test_resume_recovers_unselected_project_without_resubmission(preset_client, monkeypatch):
    client, project = preset_client
    plan = prepare(client, project, monkeypatch)
    before_selection = backend.CURRENT_PROJECT_ID
    response = client.post('/api/projects/preset-test/tasks/owned/resume')
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'owned' and response.json()['status'] == 'succeeded'
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1
    result = json.loads(plan.read_text(encoding='utf-8'))
    assert result['segments'][0]['video']['path'] == 'output/result.mp4'
    assert result['segments'][0]['prompt'] == 'keep this edit'
    assert backend.CURRENT_PROJECT_ID == before_selection == 'other'


def test_wrong_project_and_uncertain_stop_do_not_mutate_task(preset_client, monkeypatch):
    client, project = preset_client
    prepare(client, project, monkeypatch)
    before = backend.get_task('owned')
    for action in ('stop', 'resume'):
        assert client.post(f'/api/projects/other/tasks/owned/{action}').status_code == 404
    assert client.post('/api/projects/preset-test/tasks/owned/stop').status_code == 409
    assert backend.get_task('owned') == before


def test_stop_is_targeted_and_terminal_completion_not_overwritten(preset_client, monkeypatch):
    client, project = preset_client
    prepare(client, project, monkeypatch, status='queued')
    response = client.post('/api/projects/preset-test/tasks/owned/stop')
    assert response.status_code == 200 and response.json()['stop_requested'] == 1
    assert backend.CURRENT_PROJECT_ID == 'other'
    stale = backend.get_task('owned')
    backend.set_task('owned', status='succeeded')
    result = backend.request_task_stop(stale)
    assert result['status'] == 'succeeded'


def test_event_stream_stays_scoped_when_current_selection_changes(preset_client, monkeypatch):
    client, project = preset_client
    prepare(client, project, monkeypatch, status='succeeded')
    async def read_once():
        response = await backend.project_events('preset-test')
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return json.loads(event.removeprefix('data: ').strip())
    # The fixture disables render threads; asyncio.to_thread is unnecessary
    # for this finite stream iteration, so execute its read call directly.
    async def direct(function, *args): return function(*args)
    monkeypatch.setattr(backend.asyncio, 'to_thread', direct)
    event = asyncio.run(read_once())
    assert event['project_id'] == 'preset-test'
    assert [task['id'] for task in event['tasks']] == ['owned']
    assert backend.CURRENT_PROJECT_ID == 'other'
