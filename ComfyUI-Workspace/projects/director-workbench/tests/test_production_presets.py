import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend
from backend import production_presets, h3_duration


@pytest.fixture
def preset_client(tmp_path, monkeypatch):
    project = tmp_path / 'ComfyUI-Workspace' / 'project'
    projects = project / 'projects'
    projects.mkdir(parents=True)
    (projects / 'catalog.json').write_text('{"projects": []}', encoding='utf-8')
    for name, value in {'ROOT': tmp_path, 'PROJECT': project, 'PROJECTS_ROOT': projects,
                        'PROJECT_CATALOG_PATH': projects / 'catalog.json', 'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
                        'DB_PATH': tmp_path / 'state.sqlite3', 'SNAPSHOT_ROOT': project / 'snapshots', 'INPUT_ROOT': tmp_path / 'input'}.items():
        monkeypatch.setattr(backend, name, value)
    for name in ('CURRENT_PROJECT', 'CURRENT_PROJECT_ID', 'PLAN_PATH', 'MEDIA', 'TASK_TEMPLATES', 'PIPELINE_STAGES'):
        monkeypatch.setattr(backend, name, getattr(backend, name))
    monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(backend.threading, 'Thread', NoopThread)
    client = TestClient(backend.app)
    response = client.post('/api/projects/create', json={'series': '测试', 'title': '新作', 'project_id': 'preset-test', 'creation_mode': 'advanced'})
    assert response.status_code == 200, response.text
    return client, project


def install(client):
    response = client.post('/api/projects/preset-test/production-presets/h3-guided')
    assert response.status_code == 200, response.text
    return response.json()['project']


def add_shot(client, project, shot_id, with_audio=True):
    assets = project / 'workspaces' / 'preset-test' / 'assets'
    for name in (f'{shot_id}.png', f'{shot_id}.wav'):
        (assets / name).write_bytes(b'test asset')
    image = str(assets / f'{shot_id}.png')
    response = client.post('/api/projects/preset-test/plan/segments', json={'segment_id': shot_id, 'duration_seconds': 5, 'prompt': f'{shot_id} waves', 'first_frame': image, 'last_frame': image})
    assert response.status_code == 200, response.text
    if with_audio:
        assert client.patch(f'/api/projects/preset-test/plan/segments/{shot_id}', json={'audio_guide': str(assets / f'{shot_id}.wav')}).status_code == 200


def test_recipe_has_no_work_specific_inputs_and_only_connected_nodes():
    graph = production_presets.load_recipe()
    assert len(graph) == 27
    for node in graph.values():
        if node['class_type'] == 'LoadImage': assert node['inputs']['image'] == ''
        if node['class_type'] == 'LoadAudio': assert node['inputs']['audio'] == ''
        for value in node['inputs'].values():
            if isinstance(value, list): assert value[0] in graph
    assert graph['92']['inputs']['filename_prefix'] == ''
    assert graph['105:104']['inputs']['prompt'] == ''


def test_create_shot_keeps_guide_and_delivery_audio_separate(preset_client):
    client, project = preset_client
    install(client)
    assets = project / 'workspaces/preset-test/assets'
    for name in ('first.png', 'guide.wav', 'master.wav'):
        (assets / name).write_bytes(b'test media')
    response = client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'Character waves',
        'first_frame': str(assets / 'first.png'), 'last_frame': str(assets / 'first.png'),
        'audio_guide': str(assets / 'guide.wav'), 'delivery_master': str(assets / 'master.wav'),
    })
    assert response.status_code == 200, response.text
    audio = response.json()['segment']['audio']
    assert audio == {'guide': str(assets / 'guide.wav'), 'delivery_master': str(assets / 'master.wav')}
    checked = client.post('/api/projects/preset-test/pipeline/motion-generation/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert checked.json()['materials']['guide'] == str(assets / 'guide.wav')


def test_install_is_repeatable_and_keeps_existing_project_data(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    before = client.get('/api/plan').json()
    manifest = install(client)
    workflow = backend.resolve_registered_path(manifest['production_preset']['workflow'])
    assert workflow.is_file()
    assert client.get('/api/plan').json() == before
    again = client.post('/api/projects/preset-test/production-presets/h3-guided').json()
    assert again['already_installed']
    assert again['project']['production_preset'] == manifest['production_preset']
    assert len(again['project']['pipeline']) == 1


def test_incomplete_shot_and_wrong_duration_cannot_submit(preset_client):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01', with_audio=False)
    assert client.post('/api/tasks', json={'asset_id': 'S01'}).status_code == 422
    assert client.get('/api/tasks').json() == []
    client.patch('/api/projects/preset-test/plan/segments/S01', json={'duration_seconds': 30, 'audio_guide': str(project / 'workspaces/preset-test/assets/S01.wav')})
    assert client.post('/api/tasks', json={'asset_id': 'S01'}).status_code == 422
    assert client.get('/api/tasks').json() == []


def test_default_recipe_freezes_each_shots_own_materials(preset_client):
    client, project = preset_client
    install(client)
    for shot in ('S01', 'S02'): add_shot(client, project, shot)
    response = client.post('/api/batches', json={'asset_ids': ['S01', 'S02']})
    assert response.status_code == 200, response.text
    tasks = client.get('/api/tasks').json()
    assert len(tasks) == 2
    for task in tasks:
        graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
        shot = task['asset_id']
        staged = task['payload']['execution_snapshot']['staged_materials']
        first = next(item for item in staged if item['node_id'] == '114')
        audio = next(item for item in staged if item['node_id'] == 'aigc:load-audio-guide')
        assert first['source'].endswith(f'{shot}.png')
        assert audio['source'].endswith(f'{shot}.wav')
        assert graph['114']['inputs']['image'] == first['reference']
        assert (backend.INPUT_ROOT / first['reference']).read_bytes() == b'test asset'
        assert graph['105:104']['inputs']['prompt'] == f'{shot} waves'
        assert f'preset-test/video/{shot}-' in graph['92']['inputs']['filename_prefix']


def test_custom_stage_is_not_overwritten(preset_client):
    client, _ = preset_client
    manifest = backend.load_project_manifest('preset-test')
    manifest['pipeline'] = [{'id': 'motion-generation', 'title': '已有方案'}]
    backend.save_project_manifest(manifest)
    assert client.post('/api/projects/preset-test/production-presets/h3-guided').status_code == 409
    assert backend.load_project_manifest('preset-test')['pipeline'] == manifest['pipeline']


def test_single_submission_uses_preset_without_manual_workflow(preset_client):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01')
    response = client.post('/api/tasks', json={'asset_id': 'S01', 'prompt': 'new director prompt'})
    assert response.status_code == 200, response.text
    task = response.json()
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['105:104']['inputs']['prompt'] == 'new director prompt'
    assert len(task['payload']['execution_snapshot']['applied_bindings']) == 7
    assert client.get('/api/plan').json()['segments'][0]['workflow'] is None


def test_catalog_reports_missing_models_instead_of_claiming_ready(tmp_path):
    catalog = production_presets.catalog(tmp_path)
    assert catalog['available']  # recipe can be configured offline
    assert len(catalog['missing_models']) == 5


def test_native_landscape_preset_freezes_first_frame_without_audio_guide(preset_client):
    client, project = preset_client
    preset_id = production_presets.NATIVE_LANDSCAPE_PRESET_ID
    catalog = client.get('/api/production-presets').json()['presets']
    assert any(item['id'] == preset_id for item in catalog)
    response = client.post(f'/api/projects/preset-test/production-presets/{preset_id}')
    assert response.status_code == 200, response.text
    manifest = response.json()['project']
    graph = json.loads(backend.resolve_registered_path(manifest['production_preset']['workflow']).read_text(encoding='utf-8'))
    assert graph['115']['inputs'] == {'aspect_ratio': '16:9 (Widescreen)', 'megapixels': 0.4, 'multiple': 32}
    assert graph['105:11']['inputs']['vae_name'] == 'minimax_h3_video_vae_fp16.safetensors'
    assert graph['105:16']['inputs']['conditioning'] == ['105:104', 0]
    assert 'aigc:load-audio-guide' not in graph
    assert 'aigc:add-audio-guide' not in graph
    for node in graph.values():
        for value in node['inputs'].values():
            if isinstance(value, list):
                assert value[0] in graph
    assets = project / 'workspaces/preset-test/assets'
    (assets / 'mouse.png').write_bytes(b'mouse input')
    segment = client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 5, 'prompt': 'Product mouse in dark studio',
        'first_frame': str(assets / 'mouse.png'),
    })
    assert segment.status_code == 200, segment.text
    stage_id = production_presets.stage_id(preset_id)
    checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert 'guide' not in checked.json()['materials']
    queued = client.post('/api/tasks', json={'asset_id': 'S01'})
    assert queued.status_code == 200, queued.text
    frozen = json.loads(Path(queued.json()['payload']['workflow']).read_text(encoding='utf-8'))
    assert frozen['114']['inputs']['image'].endswith('.png')
    assert frozen['105:104']['inputs']['prompt'] == 'Product mouse in dark studio'
    assert frozen['115']['inputs']['aspect_ratio'] == '16:9 (Widescreen)'


def test_native_square_preset_is_independent_and_freezes_640_square_graph(preset_client):
    client, project = preset_client
    landscape_id = production_presets.NATIVE_LANDSCAPE_PRESET_ID
    square_id = production_presets.NATIVE_SQUARE_PRESET_ID
    original = client.post(f'/api/projects/preset-test/production-presets/{landscape_id}')
    assert original.status_code == 200, original.text
    landscape_stage = original.json()['project']['pipeline'][0]
    add_shot(client, project, 'S01', with_audio=False)
    before_plan = client.get('/api/plan').json()

    listed = client.get('/api/production-presets').json()['presets']
    assert next(item for item in listed if item['id'] == square_id)['available']
    response = client.post(f'/api/projects/preset-test/production-presets/{square_id}')
    assert response.status_code == 200, response.text
    manifest = response.json()['project']
    assert manifest['pipeline'][0] == landscape_stage
    assert manifest['pipeline'][1]['id'] == production_presets.stage_id(square_id)
    assert client.get('/api/plan').json() == before_plan

    graph = json.loads(backend.resolve_registered_path(manifest['production_preset']['workflow']).read_text(encoding='utf-8'))
    assert graph['115']['inputs'] == {'aspect_ratio': '1:1 (Square)', 'megapixels': 0.4, 'multiple': 32}
    assert graph['105:104']['inputs']['width'] == ['115', 0]
    assert graph['105:104']['inputs']['height'] == ['115', 1]
    assert graph['105:104']['inputs']['first_frame'] == ['114', 0]
    assert graph['105:11']['inputs']['vae_name'] == 'minimax_h3_video_vae_fp16.safetensors'
    assert graph['105:16']['inputs']['conditioning'] == ['105:104', 0]
    assert 'ip-video:last-frame:2' not in graph
    assert 'aigc:load-audio-guide' not in graph
    assert 'aigc:add-audio-guide' not in graph
    assert round((0.4 * 1024 * 1024) ** 0.5 / 32) * 32 == 640
    assert {spec['id'] for spec in manifest['pipeline'][1]['input_specs']}.isdisjoint({'last-frame', 'guide'})

    stage_id = production_presets.stage_id(square_id)
    checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert 'guide' not in checked.json()['materials']
    queued = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'pipeline_stage_id': stage_id})
    assert queued.status_code == 200, queued.text
    frozen = json.loads(Path(queued.json()['payload']['workflow']).read_text(encoding='utf-8'))
    assert frozen['92']['_meta']['director_preset'] == square_id
    assert frozen['115']['inputs'] == graph['115']['inputs']
    assert frozen['105:104']['inputs']['prompt'] == 'S01 waves'
    assert frozen['114']['inputs']['image'].endswith('.png')

    changed = client.patch('/api/projects/preset-test/plan/segments/S01', json={'duration_seconds': 10})
    assert changed.status_code == 200, changed.text
    accepted = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={'asset_id': 'S01', 'values': {}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['h3_duration']['frame_count'] == 243


def test_production_square_duration_previews_grid_without_changing_plan_or_recipe(preset_client):
    client, project = preset_client
    square_id = production_presets.NATIVE_SQUARE_PRESET_ID
    experiment_id = square_id
    production_graph = production_presets.load_recipe(square_id)
    assert next(spec for spec in production_presets.input_specs(square_id) if spec['id'] == 'duration')['max'] == 15
    add_shot(client, project, 'S01', with_audio=False)
    original_plan = client.get('/api/plan').json()
    installed = client.post(f'/api/projects/preset-test/production-presets/{experiment_id}')
    assert installed.status_code == 200, installed.text
    assert client.get('/api/plan').json() == original_plan
    assert production_presets.load_recipe(square_id) == production_graph
    stage_id = production_presets.stage_id(experiment_id)
    stage = next(item for item in installed.json()['project']['pipeline'] if item['id'] == stage_id)
    duration_spec = next(item for item in stage['input_specs'] if item['id'] == 'duration')
    assert (duration_spec['min'], duration_spec['max'], duration_spec['step']) == (4, 15, 0.1)
    graph = json.loads(backend.resolve_registered_path(stage['execution']['references'][0]).read_text(encoding='utf-8'))
    assert graph['115']['inputs'] == production_graph['115']['inputs']
    assert graph['105:107']['inputs'] == production_graph['105:107']['inputs']

    expected_frames = {4: 107, 5: 124, 8: 192, 10: 243, 12: 294, 15: 362}
    for seconds, frames in expected_frames.items():
        changed = client.patch('/api/projects/preset-test/plan/segments/S01', json={'duration_seconds': seconds})
        assert changed.status_code == 200, changed.text
        checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={
            'asset_id': 'S01', 'values': {'duration': seconds, 'seed': 42},
        })
        assert checked.status_code == 200, checked.text
        preview = checked.json()['h3_duration']
        assert (preview['requested_seconds'], preview['frame_count'], preview['playback_seconds']) == (seconds, frames, round(frames / 24, 3))
        assert (preview['width'], preview['height'], preview['fps']) == (640, 640, 24)
        assert preview['first_frame'].endswith('S01.png')
        assert preview['prompt'] == 'S01 waves'
        assert checked.json()['binding_count'] == 5


def test_production_square_duration_rejects_out_of_range_or_invalid_values_before_task(preset_client):
    client, project = preset_client
    experiment_id = production_presets.NATIVE_SQUARE_PRESET_ID
    stage_id = production_presets.stage_id(experiment_id)
    assert client.post(f'/api/projects/preset-test/production-presets/{experiment_id}').status_code == 200
    add_shot(client, project, 'S01', with_audio=False)
    for value in (3.9, 15.1, 30, 'nan', True):
        checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={
            'asset_id': 'S01', 'values': {'duration': value},
        })
        assert checked.status_code == 422, (value, checked.text)
        submitted = client.post('/api/projects/preset-test/tasks', json={
            'asset_id': 'S01', 'pipeline_stage_id': stage_id, 'pipeline_values': {'duration': value},
        })
        assert submitted.status_code == 422, (value, submitted.text)
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


@pytest.mark.parametrize('preset_id', production_presets.PRESET_IDS)
def test_existing_h3_production_presets_accept_story_driven_fractional_duration(preset_client, preset_id):
    client, project = preset_client
    add_shot(client, project, 'S01')
    installed = client.post(f'/api/projects/preset-test/production-presets/{preset_id}')
    assert installed.status_code == 200, installed.text
    stage_id = production_presets.stage_id(preset_id)
    # Simulate a recipe installed by an older server: only its in-memory
    # contract is refreshed, while the saved graph and shot remain untouched.
    manifest = installed.json()['project']
    stage = next(item for item in manifest['pipeline'] if item['id'] == stage_id)
    next(item for item in stage['input_specs'] if item['id'] == 'duration').update(min=5, max=5, step=5)
    assert next(item for item in backend.pipeline_contract(stage, manifest)['input_specs'] if item['id'] == 'duration')['max'] == 15
    assert client.patch('/api/projects/preset-test/plan/segments/S01', json={'duration_seconds': 8.5}).status_code == 200
    checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={
        'asset_id': 'S01', 'values': {'duration': 8.5},
    })
    assert checked.status_code == 200, checked.text
    assert checked.json()['h3_duration']['requested_seconds'] == 8.5
    assert checked.json()['h3_duration']['frame_count'] == h3_duration.plan(8.5)['frame_count']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


@pytest.mark.parametrize('preset_id', [production_presets.NATIVE_LANDSCAPE_PRESET_ID,
                                       production_presets.NATIVE_SQUARE_PRESET_ID])
def test_h3_plan_rejects_unusable_duration_before_saving(preset_client, preset_id):
    client, project = preset_client
    installed = client.post(f'/api/projects/preset-test/production-presets/{preset_id}')
    assert installed.status_code == 200, installed.text
    add_shot(client, project, 'S01', with_audio=False)
    path = '/api/projects/preset-test/plan'
    original = client.get(path).json()
    for duration in (3.9, 15.1):
        rejected = client.patch(path + '/segments/S01', json={'duration_seconds': duration})
        assert rejected.status_code == 422, rejected.text
        assert client.get(path).json() == original
        rejected = client.post(path + '/segments', json={'segment_id': 'S02', 'duration_seconds': duration})
        assert rejected.status_code == 422, rejected.text
        assert client.get(path).json() == original
    for duration in (4, 7.3, 8.5, 15):
        accepted = client.patch(path + '/segments/S01', json={'duration_seconds': duration})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['segment']['duration_seconds'] == duration
    assert client.get(path).json()['segments'][0]['duration_seconds'] == 15
    for operation, values in [('split', {'split_at_seconds': 3.9}),
                              ('merge', {'target_id': 'S02'})]:
        if operation == 'merge':
            assert client.post(path + '/segments', json={'segment_id': 'S02', 'duration_seconds': 4}).status_code == 200
        before = client.get(path).json()
        rejected = client.post(path + '/segments/S01/operate', json={'operation': operation, **values})
        assert rejected.status_code == 422, rejected.text
        assert client.get(path).json() == before


def test_generic_plan_keeps_non_h3_duration_range(preset_client):
    client, _ = preset_client
    path = '/api/projects/preset-test/plan/segments'
    created = client.post(path, json={'segment_id': 'S01', 'duration_seconds': 3.9})
    assert created.status_code == 200, created.text
    updated = client.patch(path + '/S01', json={'duration_seconds': 15.1})
    assert updated.status_code == 200, updated.text
    assert updated.json()['segment']['duration_seconds'] == 15.1


def test_production_square_duration_freezes_graph_and_idempotent_receipt(preset_client):
    client, project = preset_client
    experiment_id = production_presets.NATIVE_SQUARE_PRESET_ID
    stage_id = production_presets.stage_id(experiment_id)
    assert client.post(f'/api/projects/preset-test/production-presets/{experiment_id}').status_code == 200
    add_shot(client, project, 'S01', with_audio=False)
    assert client.patch('/api/projects/preset-test/plan/segments/S01', json={'duration_seconds': 12}).status_code == 200
    body = {'asset_id': 'S01', 'pipeline_stage_id': stage_id, 'pipeline_values': {'duration': 12, 'seed': 42},
            'idempotency_key': 'square-duration-12-seed-42'}
    response = client.post('/api/projects/preset-test/tasks', json=body)
    assert response.status_code == 200, response.text
    task = response.json()
    assert client.post('/api/projects/preset-test/tasks', json=body).json()['id'] == task['id']
    assert client.get('/api/projects/preset-test/submission-receipt', params={'key': body['idempotency_key']}).json()['id'] == task['id']
    conflicting = client.post('/api/projects/preset-test/tasks', json={**body, 'pipeline_values': {'duration': 10, 'seed': 42}})
    assert conflicting.status_code == 409
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1
    assert task['payload']['h3_duration']['frame_count'] == 294
    assert task['payload']['execution_snapshot']['h3_duration']['playback_seconds'] == 12.25
    frozen = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert frozen['105:111']['inputs']['value'] == 12
    assert frozen['105:107']['inputs']['expression'] == production_presets.load_recipe(experiment_id)['105:107']['inputs']['expression']
    assert frozen['115']['inputs']['aspect_ratio'] == '1:1 (Square)'
    assert frozen['105:104']['inputs']['prompt'] == 'S01 waves'
    assert frozen['114']['inputs']['image'].endswith('.png')


def test_production_square_duration_rejects_a_changed_canvas_before_submission(preset_client):
    client, project = preset_client
    experiment_id = production_presets.NATIVE_SQUARE_PRESET_ID
    stage_id = production_presets.stage_id(experiment_id)
    installed = client.post(f'/api/projects/preset-test/production-presets/{experiment_id}')
    assert installed.status_code == 200
    add_shot(client, project, 'S01', with_audio=False)
    workflow = backend.resolve_registered_path(installed.json()['project']['production_preset']['workflow'])
    graph = json.loads(workflow.read_text(encoding='utf-8'))
    graph['115']['inputs']['aspect_ratio'] = '16:9 (Widescreen)'
    workflow.write_text(json.dumps(graph), encoding='utf-8')
    for route, body in [
        (f'/api/projects/preset-test/pipeline/{stage_id}/validate', {'asset_id': 'S01', 'values': {}}),
        ('/api/projects/preset-test/tasks', {'asset_id': 'S01', 'pipeline_stage_id': stage_id}),
    ]:
        response = client.post(route, json=body)
        assert response.status_code == 422, response.text
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_assembly_rejects_old_duration_candidate_without_rewriting_it(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01', with_audio=False)
    manifest = client.post('/api/projects/preset-test/production-presets/h3-first-native-square').json()['project']
    manifest.setdefault('assembly', {})['builder'] = str(Path(__file__).resolve().parents[1] / 'tools/build_assembly_workflow.py')
    document, plan_path = backend.load_project_plan('preset-test')
    segment = document['segments'][0]
    old_video = project / 'workspaces/preset-test/assets/old.mp4'
    old_video.write_bytes(b'existing candidate')
    segment['video'] = {'path': str(old_video)}
    segment['current_version_task_id'] = 'old-task'
    segment['generation_history'] = [{'task_id': 'old-task', 'snapshot': {'duration_seconds': 5}, 'video_path': str(old_video)}]
    segment['duration_seconds'] = 8
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='当前视频按旧时长'):
        backend.build_assembly_snapshot(manifest)
    assert old_video.read_bytes() == b'existing candidate'
    assert backend.load_project_plan('preset-test')[0]['segments'][0]['generation_history'] == segment['generation_history']


def test_square_duration_history_timing_separates_queue_and_execution(monkeypatch):
    entry = {'prompt': [0, 'receipt', {}, {'create_time': 100000}, []], 'status': {
        'status_str': 'success', 'messages': [
            ['execution_start', {'timestamp': 103000}],
            ['execution_success', {'timestamp': 321298}],
        ]}, 'outputs': {}}
    timing = h3_duration.history_timing(entry)
    assert timing['queue_wait_seconds'] == 3.0
    assert timing['comfy_execution_seconds'] == 218.298
    monkeypatch.setattr(backend, 'request_json', lambda path: {'receipt': entry})
    done = backend.history_done('receipt')
    assert done['comfy_timing'] == timing
    monkeypatch.setattr(h3_duration, 'probe_video', lambda path: {'duration_seconds': 10.125, 'frames': 243, 'width': 640, 'height': 640})
    monkeypatch.setattr(backend.Path, 'is_file', lambda path: True)
    result = backend.attach_h3_duration_result(
        {**done, 'outputs': ['test.mp4']}, {'h3_duration': h3_duration.plan(10)})
    assert result['h3_duration']['comfy_seconds_per_observed_output_second'] == round(218.298 / 10.125, 3)


def test_duration_timing_missing_history_fields_is_explicitly_unavailable():
    timing = h3_duration.history_timing({'status': {'messages': []}})
    assert timing['queue_wait_seconds'] is None
    assert timing['comfy_execution_seconds'] is None
    assert set(timing) == {'source', 'queued_at_ms', 'execution_started_at_ms', 'execution_ended_at_ms', 'queue_wait_seconds', 'comfy_execution_seconds'}
