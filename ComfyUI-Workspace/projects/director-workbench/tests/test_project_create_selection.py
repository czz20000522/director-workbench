from backend import app as backend
from test_production_presets import preset_client


def test_create_without_selection_preserves_active_and_default_project(preset_client):
    client, project = preset_client
    before = client.get('/api/project').json()
    default = backend.load_project_catalog()['default_project_id']
    response = client.post('/api/projects/create', json={
        'series': 'Agent 系列', 'title': '新作品', 'project_id': 'agent-new', 'select': False,
    })
    assert response.status_code == 200, response.text
    manifest = response.json()['project']
    assert manifest['id'] == 'agent-new'
    assert client.get('/api/project').json() == before
    assert backend.load_project_catalog()['default_project_id'] == default
    assert client.get('/api/projects/agent-new').json()['id'] == 'agent-new'
    assert (project / 'workspaces/agent-new/director.project.json').is_file()


def test_legacy_create_still_selects_new_project(preset_client):
    client, _ = preset_client
    response = client.post('/api/projects/create', json={
        'series': '界面系列', 'title': '下一集', 'project_id': 'ui-new',
    })
    assert response.status_code == 200
    assert client.get('/api/project').json()['id'] == 'ui-new'
    assert backend.load_project_catalog()['default_project_id'] == 'ui-new'
