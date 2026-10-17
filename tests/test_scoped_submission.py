import json
from pathlib import Path
from backend import app as backend
from test_production_presets import preset_client, install, add_shot


def prepare(client, project):
    install(client)
    add_shot(client, project, 'S01')
    original = backend.load_project_manifest('preset-test')
    image = project / 'workspaces/preset-test/assets/S01.png'
    original['assets'] = [{'id': 'hero', 'image_path': str(image)}]
    backend.save_project_manifest(original)
    assert client.post('/api/projects/create', json={'series': '测试', 'title': 'other', 'project_id': 'other'}).status_code == 200
    other = backend.load_project_manifest('other')
    other_image = project / 'workspaces/other/assets/other.png'
    other_image.write_bytes(b'wrong project')
    other['assets'] = [{'id': 'hero', 'image_path': str(other_image)}]
    backend.save_project_manifest(other)
    backend.apply_project_manifest(other)
    return image


def test_scoped_preflight_resolves_target_material_and_does_not_submit(preset_client):
    client, project = preset_client
    image = prepare(client, project)
    result = client.post('/api/projects/preset-test/pipeline/motion-generation/validate', json={'asset_id': 'S01', 'values': {'first-frame': 'hero'}})
    assert result.status_code == 200, result.text
    assert result.json()['materials']['first-frame'] == str(image)
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
    assert client.get('/api/projects/preset-test/pipeline').json()['stages'][0]['id'] == 'motion-generation'
    assert client.get('/api/project').json()['id'] == 'other'


def test_scoped_submission_freezes_target_without_switching_project(preset_client):
    client, project = preset_client
    image = prepare(client, project)
    response = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'pipeline_values': {'first-frame': 'hero'}})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['project_id'] == 'preset-test'
    payload = task['payload']
    assert payload['project_id'] == 'preset-test' and 'preset-test' in payload['plan_path']
    assert payload['execution_snapshot']['materials']['first-frame'] == str(image)
    graph = json.loads(Path(payload['workflow']).read_text(encoding='utf-8'))
    assert graph['105:104']['inputs']['prompt'] == 'S01 waves'
    assert graph['92']['inputs']['filename_prefix'].startswith('导演工作台工作目录/preset-test/')
    assert client.get('/api/project').json()['id'] == 'other'
    assert client.get('/api/projects/other/tasks').json()['tasks'] == []


def test_target_without_recipe_does_not_use_selected_project_recipe(preset_client):
    client, project = preset_client
    prepare(client, project)
    backend.apply_project_manifest(backend.load_project_manifest('preset-test'))
    response = client.post('/api/projects/other/tasks', json={'asset_id': 'S01'})
    assert response.status_code == 400
    assert client.get('/api/projects/other/tasks').json()['tasks'] == []
    assert client.post('/api/projects/missing/tasks', json={'asset_id': 'S01'}).status_code == 404
