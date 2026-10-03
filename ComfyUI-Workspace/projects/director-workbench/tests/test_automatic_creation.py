"""CPU-only actual HTTP/MCP business calls, isolated projects and fake dispatch."""
import asyncio
import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from backend import app as backend, production_presets, guide
from test_production_presets import preset_client
from test_private_workbench import private_workbench

REAL_THREAD = threading.Thread


def seed_test_models(root):
    # Empty model filenames in the isolated root test availability contracts
    # only; neither real weights nor model loading is involved.
    directories = {'unet_name': 'diffusion_models', 'clip_name': 'text_encoders', 'vae_name': 'vae', 'lora_name': 'loras'}
    for node in production_presets.load_recipe().values():
        for key, directory in directories.items():
            if node['inputs'].get(key):
                path = root / 'ComfyUI-Shared' / 'models' / directory / node['inputs'][key]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'isolated model-availability fixture')


@pytest.fixture(autouse=True)
def isolated_model_availability(request, tmp_path):
    fixture = next((name for name in ('preset_client', 'private_workbench') if name in request.fixturenames), None)
    if fixture:
        request.getfixturevalue(fixture)
        assert backend.ROOT.is_relative_to(tmp_path)
        seed_test_models(backend.ROOT)


def automatic(client):
    manifest = backend.require_project_manifest('preset-test')
    manifest['default_creation_mode'] = 'auto'
    backend.save_project_manifest(manifest)


def create(client, **changes):
    return client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 4, 'prompt': 'A quiet antique shop, a clock ticks.', **changes})


@pytest.mark.parametrize('aspect,size', [('16:9', (864, 480)), ('1:1', (640, 640)), ('9:16', (480, 864))])
@pytest.mark.parametrize('duration,frames', [(4,107), (7.3,175), (15,362)])
def test_text_only_from_saved_intent_without_install_or_stage(preset_client, aspect, size, duration, frames):
    client, _ = preset_client
    automatic(client)
    saved = create(client, duration_seconds=duration, generation={'aspect_ratio': aspect, 'seed': 123})
    assert saved.status_code == 200, saved.text
    checked = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    data = checked.json()
    assert data['valid'] and data['materials'] == {}
    profile = data['h3_duration']
    assert profile['generation_mode'] == 'text_to_video'
    assert profile['first_frame'] is None and profile['first_frame_node_id'] is None
    assert profile['frame_count'] == frames and (profile['width'], profile['height']) == size
    posted = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'text-task', 'expected_revision': saved.json()['plan']['revision']})
    assert posted.status_code == 200, posted.text
    task = posted.json()
    snapshot = task['payload']['execution_snapshot']
    graph = json.loads(Path(snapshot['api_graph']).read_text(encoding='utf-8'))
    assert snapshot['materials'] == {} and snapshot['staged_materials'] == []
    assert snapshot['parameters']['seed'] == graph['105:15']['inputs']['noise_seed'] == 123
    assert snapshot['resolved_generation']['selection_basis'] == {'first_frame_bound': False, 'last_frame_bound': False, 'performance_audio_bound': False}
    assert graph['105:104']['inputs']['prompt'] == 'A quiet antique shop, a clock ticks.'
    assert not any(node['class_type'] in {'LoadImage', 'LoadAudio'} for node in graph.values())
    for node in graph.values():
        for value in node['inputs'].values():
            if isinstance(value, list):
                assert value[0] in graph
    # Restart-style read and editing never reroute an already frozen graph.
    before = Path(snapshot['api_graph']).read_bytes()
    assert client.patch('/api/projects/preset-test/plan/segments/S01', json={'prompt': 'An edited idea.'}).status_code == 200
    retry = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'text-task', 'expected_revision': saved.json()['plan']['revision']})
    assert retry.status_code == 200 and retry.json()['id'] == task['id']
    assert Path(snapshot['api_graph']).read_bytes() == before
    assert client.get(f"/api/projects/preset-test/tasks/{task['id']}").json()['id'] == task['id']
    steps = guide.steps(client.get('/api/projects/preset-test/state').json()['readiness'], has_project=True)
    assert next(row for row in steps if row['id'] == 'preset')['applicable'] is False
    materials = next(row for row in steps if row['id'] == 'materials')
    assert materials['applicable'] is False and materials['completed'] is False


@pytest.mark.parametrize('changes', [{'duration_seconds': 3.9}, {'duration_seconds': 15.1}, {'prompt': ' '}, {'generation': {'aspect_ratio': '4:3'}}, {'generation': {'seed': True}}, {'generation': {'seed': 1.2}}])
def test_illegal_high_level_input_has_no_plan_or_install_side_effect(preset_client, changes):
    client, _ = preset_client
    automatic(client)
    manifest = backend.require_project_manifest('preset-test')
    before = client.get('/api/projects/preset-test/plan').json()
    response = create(client, **changes)
    assert response.status_code == 422, response.text
    assert client.get('/api/projects/preset-test/plan').json() == before
    assert backend.require_project_manifest('preset-test')['pipeline'] == manifest['pipeline']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


@pytest.mark.parametrize('aspect', ['16:9', '1:1', '9:16'])
@pytest.mark.parametrize('last', [False, True])
def test_explicit_start_and_end_controls_share_the_normal_pipeline(preset_client, aspect, last):
    client, project = preset_client
    automatic(client)
    first = project / 'workspaces/preset-test/assets/first.png'
    first.write_bytes(b'isolated image fixture')
    assert create(client, first_frame=str(first), last_frame=str(first) if last else None, generation={'aspect_ratio': aspect}).status_code == 200
    response = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert response.status_code == 200, response.text
    assert response.json()['materials']['first-frame'] == str(first)
    assert ('last-frame' in response.json()['materials']) == last
    assert response.json()['h3_duration']['generation_mode'] == 'image_to_video'
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'frames'}).json()
    assert len(task['payload']['execution_snapshot']['staged_materials']) == (2 if last else 1)


def test_library_and_master_do_not_become_guides_and_unsupported_controls_are_not_dropped(preset_client):
    client, project = preset_client
    automatic(client)
    folder = project / 'workspaces/preset-test/assets'
    first, sound = folder / 'unused.png', folder / 'master.wav'
    first.write_bytes(b'image'); sound.write_bytes(b'audio')
    assert create(client, delivery_master=str(sound)).status_code == 200
    response = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert response.json()['materials'] == {} and response.json()['h3_duration']['generation_mode'] == 'text_to_video'
    before = client.get('/api/projects/preset-test/plan').json()
    for changes in ({'last_frame': str(first)}, {'audio_guide': str(sound)}, {'generation': {'sound_mode': 'locked_dialogue'}}, {'expected_revision': 0}):
        failure = client.patch('/api/projects/preset-test/plan/segments/S01', json=changes)
        assert failure.status_code in (422,409), failure.text
        assert client.get('/api/projects/preset-test/plan').json() == before
    assert client.patch('/api/projects/preset-test/plan/segments/S01', json={'first_frame': str(first), 'audio_guide': str(sound), 'generation': {'aspect_ratio': '9:16'}}).status_code == 200
    response = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert response.status_code == 200 and response.json()['materials']['guide'] == str(sound)
    assert client.patch('/api/projects/preset-test/plan/segments/S01', json={'generation': {'aspect_ratio': '9:16', 'sound_mode': 'locked_dialogue'}}).status_code == 200
    response = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert response.status_code == 200 and response.json()['stage_id'] == production_presets.stage_id(production_presets.LOCKED_PRESET_ID)


def test_public_default_project_and_batch_version_contract(preset_client):
    client, _ = preset_client
    response = client.post('/api/projects/create', json={'series':'Test', 'title':'Fresh', 'project_id':'fresh'})
    assert response.json()['project']['default_creation_mode'] == 'auto'
    saved = client.post('/api/projects/fresh/plan/segments', json={'prompt':'A clock ticks', 'duration_seconds':4})
    assert saved.status_code == 200, saved.text
    assert saved.json()['segment']['resolved_generation']['generation_mode'] == 'text_to_video'
    assert client.post('/api/projects/fresh/tasks', json={'asset_id':'S01','expected_revision':0}).status_code == 409
    assert client.post('/api/projects/fresh/batches', json={'asset_ids':['S01'],'expected_revision':0}).status_code == 409
    response = client.post('/api/projects/fresh/batches', json={'asset_ids':['S01'],'expected_revision':saved.json()['plan']['revision'],'idempotency_key':'batch'})
    assert response.status_code == 200, response.text
    tasks = client.get('/api/projects/fresh/tasks').json()['tasks']
    assert len(tasks) == 1 and tasks[0]['payload']['execution_snapshot']['materials'] == {}


def test_actual_mcp_uses_same_http_high_level_contract(preset_client, monkeypatch):
    from mcp import Client
    from mcp_server.server import create_server
    client, _ = preset_client
    automatic(client)
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'queue_task', lambda *args: None)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as mcp:
            result = await mcp.call_tool('create_shot', {'project_id':'preset-test','segment_id':'S01','expected_revision':0,'duration_seconds':7.3,'prompt':'Clock ticks','generation':{'aspect_ratio':'1:1','seed':89}})
            assert result.structured_content['ok'], result
            checked = await mcp.call_tool('preflight', {'project_id':'preset-test','asset_id':'S01'})
            assert checked.structured_content['ok'], checked
            http = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id':'S01','values':{}}).json()
            assert checked.structured_content['data'] == http
            args = {'project_id':'preset-test','asset_id':'S01','idempotency_key':'mcp-auto','expected_revision':http['revision']}
            task = await mcp.call_tool('submit_shot', args)
            assert task.structured_content['ok'], task
            again = await mcp.call_tool('submit_shot', args)
            assert again.structured_content['data']['id'] == task.structured_content['data']['id']
            assert task.structured_content['data']['payload']['execution_snapshot']['parameters']['seed'] == 89
    asyncio.run(verify())


def test_private_empty_accounts_ownership_and_unbound_other_assets(private_workbench, monkeypatch):
    a, b, _, _ = private_workbench
    monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})
    monkeypatch.setattr(backend, 'queue_task', lambda *args: None)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('No Comfy request permitted'))
    project = a.post('/api/projects/create', json={'title':'Automatic','series':'New'}).json()['project']
    pid = project['id']
    saved = a.post(f'/api/projects/{pid}/plan/segments', json={'prompt':'A clock ticks','duration_seconds':4})
    assert saved.status_code == 200, saved.text
    revision = saved.json()['plan']['revision']
    assert b.get(f'/api/projects/{pid}/plan').status_code == 404
    assert b.patch(f'/api/projects/{pid}/plan/segments/S01', json={'generation':{'aspect_ratio':'1:1'}}).status_code == 404
    assert b.post(f'/api/projects/{pid}/pipeline/auto/validate', json={'asset_id':'S01','values':{}}).status_code == 404
    assert b.post(f'/api/projects/{pid}/tasks', json={'asset_id':'S01','idempotency_key':'foreign'}).status_code == 404
    checked = a.post(f'/api/projects/{pid}/pipeline/auto/validate', json={'asset_id':'S01','values':{}})
    assert checked.status_code == 200 and checked.json()['valid']
    task = a.post(f'/api/projects/{pid}/tasks', json={'asset_id':'S01','expected_revision':revision,'idempotency_key':'private'})
    assert task.status_code == 200, task.text
    assert b.post(f"/api/projects/{pid}/tasks/{task.json()['id']}/stop").status_code == 404
    stopped = a.post(f"/api/projects/{pid}/tasks/{task.json()['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped'
    assert a.get(f'/api/projects/{pid}/plan').json()['revision'] == revision


def test_installed_node_no_image_branch_without_models_or_gpu():
    # Run the actual installed method body with fake clip/latent I/O only.
    # This proves branch wiring, not sampling or the real tensor latent.
    source = backend.ROOT / 'ComfyUI-Installs/第一个comfyui配置/ComfyUI/comfy_extras/nodes_minimax_h3.py'
    if not source.is_file():
        pytest.skip('Host-specific H3 node probe requires the separate ComfyUI desktop installation')
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'MiniMaxH3ImageToVideo')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'execute')
    method.decorator_list = []; method.returns = None
    scope = {'io':SimpleNamespace(NodeOutput=lambda *args:args), '_empty_av_latent':lambda *args:({'samples':'fake AV latent'},107)}
    calls=[]
    clip=SimpleNamespace(tokenize=lambda prompt,images:calls.append((prompt,images)) or 'tokens', encode_from_tokens_scheduled=lambda tokens:'conditioning')
    vae=SimpleNamespace(encode=lambda *args:pytest.fail('No image should be VAE encoded'))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(source),'exec'),scope)
    assert scope['execute'](None,clip,vae,'clock',864,480,107) == ('conditioning',{'samples':'fake AV latent'})
    assert calls == [('clock',[])]


def test_missing_models_block_same_preflight_readiness_and_submit(preset_client, monkeypatch, tmp_path):
    client, _ = preset_client
    automatic(client)
    saved = create(client)
    assert saved.status_code == 200
    before = client.get('/api/projects/preset-test/plan').json()
    catalog = production_presets.catalog
    monkeypatch.setattr(production_presets, 'catalog', lambda root, preset: catalog(tmp_path / 'missing-models', preset))
    checked = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id':'S01','values':{}})
    assert checked.status_code == 422 and checked.json()['detail']['code'] == 'creation_resources_missing'
    assert checked.json()['detail']['missing_models']
    state = client.get('/api/projects/preset-test/state').json()['readiness']
    assert not state['segments'][0]['ready']
    rejected = client.post('/api/projects/preset-test/tasks', json={'asset_id':'S01','idempotency_key':'missing'})
    assert rejected.status_code == 422
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
    assert client.get('/api/projects/preset-test/plan').json() == before


def test_auto_preflight_cannot_use_another_installed_stage(preset_client):
    client, _ = preset_client
    automatic(client)
    assert create(client).status_code == 200
    assert client.post('/api/projects/preset-test/production-presets/h3-first-native-landscape').status_code == 200
    before = client.get('/api/projects/preset-test/plan').json()
    for endpoint in ('pipeline/motion-first-native-landscape-generation/validate', 'tasks'):
        response = client.post('/api/projects/preset-test/' + endpoint, json={'asset_id':'S01', 'values':{}, 'pipeline_stage_id':'motion-first-native-landscape-generation', 'idempotency_key':'mismatch'})
        assert response.status_code == 422, response.text
    assert client.get('/api/projects/preset-test/plan').json() == before


def test_restore_old_candidate_removes_later_auto_settings(preset_client):
    from test_segment_versions import generated_version
    client, project = preset_client
    assert create(client).status_code == 200
    old_video = generated_version(client, project, 'legacy', 'Old idea', 1_780_000_000)
    automatic(client)
    changed = client.patch('/api/projects/preset-test/plan/segments/S01', json={'generation':{'mode':'auto','seed':99}, 'prompt':'New idea'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['segment']['resolved_generation']['generation_mode'] == 'text_to_video'
    restored = client.post('/api/projects/preset-test/segments/S01/versions/legacy/restore', json={'expected_plan_revision':changed.json()['plan']['revision']})
    assert restored.status_code == 200, restored.text
    assert 'generation' not in restored.json()['segment']
    assert 'resolved_generation' not in restored.json()['segment']
    assert old_video.is_file()


def complete_frozen_task(task, project):
    """Exercise the production result recorder; no executor, GPU or real media."""
    pid = task['project_id']
    manifest = backend.require_project_manifest(pid)
    video = backend.resolve_registered_path(manifest['workspace_root']) / f"assets/{task['id']}.mp4"
    video.write_bytes(b'isolated receipt fixture')
    result = {'outputs': [video.name], 'published_outputs': [str(video)]}
    with backend.db() as connection:
        connection.execute("UPDATE tasks SET status='succeeded',result=?,prompt_id=? WHERE id=?",
                           (json.dumps(result), task['id'], task['id']))
        connection.commit()
    _, plan_path = backend.load_project_plan(pid)
    backend.record_segment_result(task['asset_id'], task['id'], task['payload']['workflow'],
                                  result, plan_path, task_id=task['id'], payload=task['payload'])


@pytest.mark.parametrize('image', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_completion_preserves_recipe_and_repeat_preflight(preset_client, image, batch):
    client, project = preset_client
    automatic(client)
    first = project / 'workspaces/preset-test/assets/start.png'
    first.write_bytes(b'image fixture')
    saved = create(client, first_frame=str(first) if image else None,
                   generation={'aspect_ratio': '9:16', 'seed': 42}).json()
    original = saved['segment']
    checked = client.post('/api/projects/preset-test/pipeline/auto/validate',
                          json={'asset_id': 'S01', 'values': {}}).json()
    if batch:
        response = client.post('/api/projects/preset-test/batches',
                               json={'asset_ids': ['S01'], 'idempotency_key': 'completed-batch'})
        assert response.status_code == 200, response.text
        task = client.get('/api/projects/preset-test/tasks').json()['tasks'][0]
    else:
        response = client.post('/api/projects/preset-test/tasks',
                               json={'asset_id': 'S01', 'idempotency_key': 'completed-shot'})
        assert response.status_code == 200, response.text
        task = response.json()
    frozen = task['payload']['workflow']
    assert frozen != original['workflow']
    complete_frozen_task(task, project)
    plan = client.get('/api/projects/preset-test/plan').json()
    assert plan['segments'][0]['workflow'] == original['workflow']
    history = plan['segments'][0]['generation_history'][-1]
    assert history['workflow'] == frozen
    assert history['snapshot']['workflow'] == original['workflow']
    for stage in ('auto', checked['stage_id']):
        response = client.post(f'/api/projects/preset-test/pipeline/{stage}/validate',
                               json={'asset_id': 'S01', 'values': {}})
        assert response.status_code == 200, response.text
        assert response.json()['valid']
    assert client.get('/api/projects/preset-test/plan').json() == plan
    if not batch:
        assert client.post('/api/projects/preset-test/tasks', json={
            'asset_id': 'S01', 'idempotency_key': 'completed-shot'}).json()['id'] == task['id']
        again = client.post('/api/projects/preset-test/tasks', json={
            'asset_id': 'S01', 'idempotency_key': 'next-shot'})
        assert again.status_code == 200, again.text
        assert again.json()['id'] != task['id']


def test_old_result_does_not_overwrite_edits_and_restore_uses_original_recipe(preset_client):
    client, project = preset_client
    automatic(client)
    original = create(client).json()['segment']
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'editing'}).json()
    changed = client.patch('/api/projects/preset-test/plan/segments/S01', json={
        'prompt': 'A different idea', 'duration_seconds': 7.3, 'generation': {'aspect_ratio': '9:16', 'seed': 77}}).json()['segment']
    complete_frozen_task(task, project)
    plan = client.get('/api/projects/preset-test/plan').json()
    for key in ('prompt', 'duration_seconds', 'workflow', 'generation', 'resolved_generation'):
        assert plan['segments'][0][key] == changed[key]
    response = client.post('/api/projects/preset-test/segments/S01/versions/' + task['id'] + '/restore',
                           json={'expected_plan_revision': plan['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['segment']['workflow'] == original['workflow']
    assert response.json()['segment']['generation'] == original['generation']
    assert client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}}).json()['valid']


def overwrite_recipe_like_old_release(client, task):
    plan, path = backend.load_project_plan(task['project_id'])
    plan['segments'][0]['workflow'] = backend.root_relative_path(Path(task['payload']['workflow']))
    plan['segments'][0]['review_marker'] = 'pending_review'
    plan['assembly'] = {'status': 'stale', 'output': plan['segments'][0]['video']['path']}
    backend.write_plan_version(path, plan)
    return client.get('/api/projects/' + task['project_id'] + '/plan').json()


def test_explicit_identity_repair_preserves_state_and_returns_same_mcp_receipt(preset_client, monkeypatch):
    from mcp import Client
    from mcp_server.server import create_server
    client, project = preset_client
    automatic(client)
    original = create(client).json()['segment']
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'broken'}).json()
    complete_frozen_task(task, project)
    broken = overwrite_recipe_like_old_release(client, task)
    for _ in range(2):
        failed = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
        assert failed.status_code == 422 and failed.json()['detail']['code'] == 'creation_recipe_unresolved'
        assert not failed.json()['detail']['readiness']['ready']
        assert not client.get('/api/projects/preset-test/state').json()['readiness']['segments'][0]['ready']
        assert client.get('/api/projects/preset-test/plan').json() == broken
    endpoint = '/api/projects/preset-test/segments/S01/recipe-identity/repair'
    body = {'task_id': task['id'], 'expected_revision': broken['revision']}
    dry = client.post(endpoint, json=body)
    assert dry.status_code == 200, dry.text
    assert dry.json()['repair_needed'] and not dry.json()['changed']
    assert client.get('/api/projects/preset-test/plan').json() == broken
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as mcp:
            checked = await mcp.call_tool('repair_shot_recipe_identity', {'project_id':'preset-test', 'asset_id':'S01', **body})
            assert checked.structured_content['ok'], checked
            assert checked.structured_content['data'] == dry.json()
            fixed = await mcp.call_tool('repair_shot_recipe_identity', {'project_id':'preset-test', 'asset_id':'S01', **body, 'dry_run':False})
            assert fixed.structured_content['ok'], fixed
            assert fixed.structured_content['data']['changed']
            checked = await mcp.call_tool('preflight', {'project_id':'preset-test', 'asset_id':'S01'})
            assert checked.structured_content['ok'] and checked.structured_content['data']['valid']
    asyncio.run(verify())
    repaired = client.get('/api/projects/preset-test/plan').json()
    expected = json.loads(json.dumps(broken))
    expected['segments'][0]['workflow'] = original['workflow']
    for key in ('revision', 'updated_at', 'recipe_identity_repairs'):
        repaired.pop(key, None); expected.pop(key, None)
    assert repaired == expected
    assert client.post(endpoint, json={**body, 'dry_run':False}).status_code == 409
    assert client.post('/api/projects/preset-test/tasks', json={'asset_id':'S01','idempotency_key':'broken'}).json()['id'] == task['id']


@pytest.mark.parametrize('tamper', ['changed_inputs', 'foreign_graph', 'foreign_source', 'wrong_stage', 'not_current', 'active'])
def test_identity_repair_refuses_uncertain_sources_or_edits(preset_client, tamper):
    client, project = preset_client
    automatic(client); create(client)
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id':'S01', 'idempotency_key':'unsafe'}).json()
    complete_frozen_task(task, project)
    overwrite_recipe_like_old_release(client, task)
    document, path = backend.load_project_plan('preset-test')
    if tamper == 'changed_inputs': document['segments'][0]['prompt'] = 'edited while generating'
    if tamper == 'not_current': document['segments'][0]['current_version_task_id'] = 'other'
    payload = task['payload']
    if tamper == 'foreign_graph': payload['execution_snapshot']['api_graph'] = 'not-registered'
    if tamper == 'foreign_source': payload['execution_snapshot']['source_workflow'] = 'other-recipe'
    if tamper == 'wrong_stage': payload['execution_snapshot']['pipeline_stage_id'] = 'other-stage'
    with backend.db() as connection:
        connection.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(payload), task['id']))
        if tamper == 'active':
            connection.execute("UPDATE tasks SET status='running' WHERE id=?", (task['id'],))
        connection.commit()
    backend.write_plan_version(path, document)
    before = client.get('/api/projects/preset-test/plan').json()
    failure = client.post('/api/projects/preset-test/segments/S01/recipe-identity/repair', json={
        'task_id':task['id'], 'expected_revision':before['revision'], 'dry_run':False})
    assert failure.status_code in (404,409), failure.text
    assert client.get('/api/projects/preset-test/plan').json() == before


@pytest.mark.parametrize('tamper', ['source_fps', 'both_fps', 'source_fixed_parameter', 'source_connection'])
def test_identity_repair_rejects_drift_even_with_the_same_node_classes(preset_client, tamper):
    client, project = preset_client
    automatic(client)
    create(client)
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'drift'}).json()
    complete_frozen_task(task, project)
    before = overwrite_recipe_like_old_release(client, task)
    source = Path(task['payload']['execution_snapshot']['source_workflow'])
    frozen = Path(task['payload']['workflow'])
    graph = json.loads(source.read_text(encoding='utf-8'))
    if tamper in ('source_fps', 'both_fps'):
        graph['105:91']['inputs']['fps'] = 13
    elif tamper == 'source_fixed_parameter':
        graph['105:17']['inputs']['sampler_name'] = 'euler'
    else:
        graph['105:16']['inputs']['conditioning'] = ['105:104', 1]
    source.write_text(json.dumps(graph), encoding='utf-8')
    if tamper == 'both_fps':
        frozen_graph = json.loads(frozen.read_text(encoding='utf-8'))
        frozen_graph['105:91']['inputs']['fps'] = 13
        frozen.write_text(json.dumps(frozen_graph), encoding='utf-8')
    before_tasks = client.get('/api/projects/preset-test/tasks').json()
    before_source, before_frozen = source.read_bytes(), frozen.read_bytes()
    endpoint = '/api/projects/preset-test/segments/S01/recipe-identity/repair'
    for dry_run in (True, False):
        refused = client.post(endpoint, json={'task_id': task['id'], 'expected_revision': before['revision'], 'dry_run': dry_run})
        assert refused.status_code == 409, refused.text
        assert client.get('/api/projects/preset-test/plan').json() == before
        assert client.get('/api/projects/preset-test/tasks').json() == before_tasks
        assert source.read_bytes() == before_source and frozen.read_bytes() == before_frozen


@pytest.mark.parametrize('controls', ['text', 'first', 'frames', 'guide', 'locked'])
def test_identity_repair_accepts_recorded_freeze_changes_and_preserves_all_history(preset_client, controls):
    client, project = preset_client
    automatic(client)
    first = project / 'workspaces/preset-test/assets/first.png'
    guide_audio = project / 'workspaces/preset-test/assets/guide.wav'
    first.write_bytes(b'isolated image fixture')
    guide_audio.write_bytes(b'isolated audio fixture')
    saved = create(client, duration_seconds=7.3,
                   first_frame=str(first) if controls != 'text' else None,
                   last_frame=str(first) if controls == 'frames' else None,
                   audio_guide=str(guide_audio) if controls in ('guide', 'locked') else None,
                   generation={'aspect_ratio': '9:16', 'seed': 123,
                               'sound_mode': 'locked_dialogue' if controls == 'locked' else 'performance_reference'})
    assert saved.status_code == 200, saved.text
    original = saved.json()['segment']['workflow']
    submitted = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'valid-repair'})
    assert submitted.status_code == 200, submitted.text
    task = submitted.json()
    complete_frozen_task(task, project)
    before = overwrite_recipe_like_old_release(client, task)
    before_tasks = client.get('/api/projects/preset-test/tasks').json()
    source = Path(task['payload']['execution_snapshot']['source_workflow'])
    frozen = Path(task['payload']['workflow'])
    graph = json.loads(source.read_text(encoding='utf-8'))
    # Editable defaults and display metadata are not fixed execution inputs.
    graph['105:104']['inputs']['prompt'] = 'New default prompt'
    graph['105:15']['inputs']['noise_seed'] = 999
    graph['92'].setdefault('_meta', {})['title'] = 'Updated display title'
    source.write_text(json.dumps(graph), encoding='utf-8')
    before_source, before_frozen = source.read_bytes(), frozen.read_bytes()
    endpoint = '/api/projects/preset-test/segments/S01/recipe-identity/repair'
    body = {'task_id': task['id'], 'expected_revision': before['revision']}
    dry = client.post(endpoint, json=body)
    assert dry.status_code == 200, dry.text
    assert dry.json()['repair_needed'] and not dry.json()['changed']
    assert client.get('/api/projects/preset-test/plan').json() == before
    applied = client.post(endpoint, json={**body, 'dry_run': False})
    assert applied.status_code == 200, applied.text
    assert applied.json()['changed']
    repaired = client.get('/api/projects/preset-test/plan').json()
    checked = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert checked.json()['valid']
    assert client.get('/api/projects/preset-test/plan').json() == repaired
    expected = json.loads(json.dumps(before))
    expected['segments'][0]['workflow'] = original
    for key in ('revision', 'updated_at', 'recipe_identity_repairs'):
        repaired.pop(key, None)
        expected.pop(key, None)
    assert repaired == expected
    assert client.get('/api/projects/preset-test/tasks').json() == before_tasks
    assert source.read_bytes() == before_source and frozen.read_bytes() == before_frozen


@pytest.mark.parametrize('unavailable', ['models', 'material'])
def test_identity_repair_reuses_saved_input_and_resource_validation(preset_client, monkeypatch, unavailable):
    from fastapi import HTTPException
    client, project = preset_client
    automatic(client)
    first = project / 'workspaces/preset-test/assets/first.png'
    first.write_bytes(b'isolated image fixture')
    create(client, first_frame=str(first))
    task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'idempotency_key': 'unavailable'}).json()
    complete_frozen_task(task, project)
    before = overwrite_recipe_like_old_release(client, task)
    if unavailable == 'models':
        monkeypatch.setattr(production_presets, 'catalog', lambda *args: {'available': True, 'missing_models': ['fixture-only-model']})
    else:
        resolve = backend.resolve_pipeline_material
        def unavailable_material(value, kind, manifest=None):
            if kind == '图片':
                raise HTTPException(422, 'isolated unavailable material')
            return resolve(value, kind, manifest)
        monkeypatch.setattr(backend, 'resolve_pipeline_material', unavailable_material)
    for dry_run in (True, False):
        refused = client.post('/api/projects/preset-test/segments/S01/recipe-identity/repair', json={
            'task_id': task['id'], 'expected_revision': before['revision'], 'dry_run': dry_run})
        assert refused.status_code == 409, refused.text
        assert client.get('/api/projects/preset-test/plan').json() == before


def test_private_identity_repair_authorizes_actual_owner_only(private_workbench, monkeypatch):
    from backend.private_auth import Principal, current_principal
    from backend.user_context import SessionIdentity
    a, b, ownership, project = private_workbench
    monkeypatch.setattr(backend, 'queue_task', lambda *args: None)
    monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})
    pid = a.post('/api/projects/create', json={'series':'Private','title':'Repair'}).json()['project']['id']
    assert a.post(f'/api/projects/{pid}/plan/segments', json={'prompt':'A clock ticks','duration_seconds':4}).status_code == 200
    task = a.post(f'/api/projects/{pid}/tasks', json={'asset_id':'S01','idempotency_key':'private-repair'}).json()
    token = current_principal.set(Principal(SessionIdentity('user001', 4_000_000_000), 'fixture-only', False))
    try:
        complete_frozen_task(task, project)
        broken = overwrite_recipe_like_old_release(a, task)
    finally:
        current_principal.reset(token)
    endpoint = f'/api/projects/{pid}/segments/S01/recipe-identity/repair'
    body = {'task_id':task['id'], 'expected_revision':broken['revision'], 'dry_run':False}
    assert b.post(endpoint, json=body).status_code == 404
    assert a.get(f'/api/projects/{pid}/plan').json() == broken
    assert a.post(endpoint, json=body).status_code == 200
    checked = a.post(f'/api/projects/{pid}/pipeline/auto/validate', json={'asset_id':'S01','values':{}})
    assert checked.status_code == 200, checked.text
    assert checked.json()['valid']
