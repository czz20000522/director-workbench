"""Explicit creation contract: no auto recipe, no implicit plan writes."""
import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest
from backend import app as backend, guide, production_presets
from support import seed_test_models

REAL_THREAD = threading.Thread


@pytest.fixture(autouse=True)
def isolated_model_availability(request, tmp_path):
    fixture = next((name for name in ('preset_client', 'private_workbench') if name in request.fixturenames), None)
    if fixture:
        request.getfixturevalue(fixture)
        assert backend.ROOT.is_relative_to(tmp_path)
        seed_test_models(backend.SHARED_ROOT)


def install(client, preset_id=production_presets.PRESET_ID):
    manifest = backend.require_project_manifest('preset-test')
    if (manifest.get('production_preset') or {}).get('id') == preset_id:
        return manifest['production_preset']['workflow']
    response = client.post(f'/api/projects/preset-test/production-presets/{preset_id}')
    assert response.status_code == 200, response.text
    return backend.require_project_manifest('preset-test')['production_preset']['workflow']


def create(client, workflow=None, **changes):
    manifest = backend.require_project_manifest('preset-test')
    workflow = workflow or (manifest.get('production_preset') or {}).get('workflow')
    payload = {'segment_id': 'S01', 'duration_seconds': 4, 'prompt': 'A quiet antique shop, a clock ticks.', **changes}
    if workflow:
        payload['workflow'] = workflow
    return client.post('/api/projects/preset-test/plan/segments', json=payload)


def project_asset(project, name, data=b'fixture'):
    path = project / 'workspaces/preset-test/assets' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def create_ready(client, project, **changes):
    first = project_asset(project, 'first.png')
    guide_audio = project_asset(project, 'guide.wav')
    return create(client, first_frame=first, last_frame=first, audio_guide=guide_audio, **changes)


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


def test_project_creation_requires_explicit_mode(preset_client):
    client, _ = preset_client
    for body in ({'series': 'S', 'title': 'Missing mode', 'project_id': 'missing-mode'},
                 {'series': 'S', 'title': 'Auto mode', 'project_id': 'auto-mode', 'creation_mode': 'auto'}):
        response = client.post('/api/projects/create', json=body)
        assert response.status_code == 422, response.text
    assert client.get('/api/projects/missing-mode').status_code == 404
    assert client.get('/api/projects/auto-mode').status_code == 404
    created = client.post('/api/projects/create', json={
        'series': 'S', 'title': 'Explicit', 'project_id': 'explicit', 'creation_mode': 'advanced'})
    assert created.status_code == 200, created.text
    assert created.json()['project']['creation_mode'] == 'advanced'


def test_missing_plan_blocks_without_writing_and_installed_plan_is_used(preset_client):
    client, _ = preset_client
    before = client.get('/api/projects/preset-test/plan').json()
    blocked = create(client)
    assert blocked.status_code == 422, blocked.text
    detail = blocked.json()['detail']
    assert detail['code'] == 'creation_plan_required'
    assert detail['options']['available_presets']
    assert client.get('/api/projects/preset-test/plan').json() == before
    manifest = backend.require_project_manifest('preset-test')
    assert manifest['pipeline'] == []
    assert 'production_preset' not in manifest

    workflow = install(client, production_presets.TEXT_LANDSCAPE_PRESET_ID)
    saved = create(client, generation={'aspect_ratio': '16:9', 'seed': 7})
    assert saved.status_code == 200, saved.text
    segment = saved.json()['segment']
    assert segment['workflow'] == workflow
    assert 'resolved_generation' not in segment
    manifest = backend.require_project_manifest('preset-test')
    assert [stage['id'] for stage in manifest['pipeline']] == [production_presets.stage_id(production_presets.TEXT_LANDSCAPE_PRESET_ID)]


def test_auto_stage_and_generation_mode_are_rejected(preset_client):
    client, _ = preset_client
    install(client, production_presets.TEXT_LANDSCAPE_PRESET_ID)
    assert create(client).status_code == 200
    before = client.get('/api/projects/preset-test/plan').json()
    auto_preflight = client.post('/api/projects/preset-test/pipeline/auto/validate', json={'asset_id': 'S01', 'values': {}})
    assert auto_preflight.status_code == 422
    assert auto_preflight.json()['detail']['code'] == 'explicit_stage_required'
    auto_task = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01', 'pipeline_stage_id': 'auto'})
    assert auto_task.status_code == 422
    mode = client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'S02', 'duration_seconds': 4, 'prompt': 'text',
        'generation': {'mode': 'auto', 'aspect_ratio': '16:9'}})
    assert mode.status_code == 422
    assert client.get('/api/projects/preset-test/plan').json() == before
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_unsupported_combinations_block_instead_of_falling_back(preset_client):
    client, project = preset_client
    install(client, production_presets.TEXT_LANDSCAPE_PRESET_ID)
    before = client.get('/api/projects/preset-test/plan').json()
    first = project_asset(project, 'first.png')
    text_with_frame = create(client, first_frame=first)
    assert text_with_frame.status_code == 422, text_with_frame.text
    assert text_with_frame.json()['detail']['code'] == 'unsupported_creation_inputs'
    assert client.get('/api/projects/preset-test/plan').json() == before
    assert len(backend.require_project_manifest('preset-test')['pipeline']) == 1

    install(client, production_presets.NATIVE_LANDSCAPE_PRESET_ID)
    before = client.get('/api/projects/preset-test/plan').json()
    native_with_last = create(client, first_frame=first, last_frame=first)
    assert native_with_last.status_code == 422, native_with_last.text
    assert client.get('/api/projects/preset-test/plan').json() == before

    install(client, production_presets.PRESET_ID)
    before = client.get('/api/projects/preset-test/plan').json()
    locked_sound = create(client, generation={'sound_mode': 'locked_dialogue'})
    assert locked_sound.status_code == 422, locked_sound.text
    assert locked_sound.json()['detail']['code'] == 'preset_sound_mode_mismatch'
    adult = create(client, generation={'creative_profile': 'adult-anime'})
    assert adult.status_code == 422 and adult.json()['detail']['code'] == 'unsupported_creative_profile'
    assert client.get('/api/projects/preset-test/plan').json() == before

    uninstalled = client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'S02', 'duration_seconds': 4, 'prompt': 'text',
        'preset_id': production_presets.TEXT_SQUARE_PRESET_ID})
    assert uninstalled.status_code == 422
    assert uninstalled.json()['detail']['code'] == 'preset_not_installed'
    assert client.get('/api/projects/preset-test/plan').json() == before


@pytest.mark.parametrize('aspect,size', [('16:9', (864, 480)), ('1:1', (640, 640)), ('9:16', (480, 864))])
@pytest.mark.parametrize('duration,frames', [(4, 107), (7.3, 175), (15, 362)])
def test_explicit_text_preset_preflight_and_submit(preset_client, aspect, size, duration, frames):
    client, _ = preset_client
    preset_id = {'16:9': production_presets.TEXT_LANDSCAPE_PRESET_ID,
                 '1:1': production_presets.TEXT_SQUARE_PRESET_ID,
                 '9:16': production_presets.TEXT_PORTRAIT_PRESET_ID}[aspect]
    workflow = install(client, preset_id)
    saved = create(client, workflow=workflow, duration_seconds=duration,
                   generation={'aspect_ratio': aspect, 'seed': 123})
    assert saved.status_code == 200, saved.text
    assert saved.json()['segment']['workflow'] == workflow
    assert 'resolved_generation' not in saved.json()['segment']
    checked = client.post(f'/api/projects/preset-test/pipeline/{production_presets.stage_id(preset_id)}/validate',
                          json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    data = checked.json()
    assert data['valid'] and data['materials'] == {}
    profile = data['h3_duration']
    assert profile['generation_mode'] == 'text_to_video'
    assert profile['first_frame'] is None and profile['first_frame_node_id'] is None
    assert profile['frame_count'] == frames and (profile['width'], profile['height']) == size
    posted = client.post('/api/projects/preset-test/tasks', json={
        'asset_id': 'S01', 'idempotency_key': 'text-task',
        'pipeline_stage_id': production_presets.stage_id(preset_id),
        'expected_revision': saved.json()['plan']['revision']})
    assert posted.status_code == 200, posted.text
    snapshot = posted.json()['payload']['execution_snapshot']
    assert 'resolved_generation' not in snapshot
    assert snapshot['parameters']['seed'] == 123
    assert snapshot['materials'] == {}
    steps = guide.steps(client.get('/api/projects/preset-test/state').json()['readiness'], has_project=True)
    assert next(row for row in steps if row['id'] == 'preset')['completed'] is True


@pytest.mark.parametrize('aspect', ['16:9', '1:1', '9:16'])
@pytest.mark.parametrize('last', [False, True])
def test_explicit_start_and_end_controls_use_selected_presets(preset_client, aspect, last):
    client, project = preset_client
    preset_id = (production_presets.END_PRESET_IDS[{'16:9': 0, '1:1': 1, '9:16': 2}[aspect]]
                 if last else {'16:9': production_presets.NATIVE_LANDSCAPE_PRESET_ID,
                               '1:1': production_presets.NATIVE_SQUARE_PRESET_ID,
                               '9:16': production_presets.NATIVE_PORTRAIT_PRESET_ID}[aspect])
    workflow = install(client, preset_id)
    first = project_asset(project, 'first.png')
    saved = create(client, workflow=workflow, first_frame=first,
                   last_frame=first if last else None, generation={'aspect_ratio': aspect})
    assert saved.status_code == 200, saved.text
    stage_id = production_presets.stage_id(preset_id)
    checked = client.post(f'/api/projects/preset-test/pipeline/{stage_id}/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert checked.json()['materials']['first-frame'] == first
    assert ('last-frame' in checked.json()['materials']) == last
    assert checked.json()['h3_duration']['generation_mode'] == 'image_to_video'
    task = client.post('/api/projects/preset-test/tasks', json={
        'asset_id': 'S01', 'idempotency_key': 'frames', 'pipeline_stage_id': stage_id,
        'expected_revision': saved.json()['plan']['revision']}).json()
    assert len(task['payload']['execution_snapshot']['staged_materials']) == (2 if last else 1)


def test_locked_dialogue_requires_explicit_locked_preset(preset_client):
    client, project = preset_client
    workflow = install(client, production_presets.LOCKED_PRESET_ID)
    first = project_asset(project, 'first.png')
    guide_audio = project_asset(project, 'guide.wav')
    saved = create(client, workflow=workflow, first_frame=first, audio_guide=guide_audio,
                   generation={'sound_mode': 'locked_dialogue'})
    assert saved.status_code == 200, saved.text
    checked = client.post(f'/api/projects/preset-test/pipeline/{production_presets.stage_id(production_presets.LOCKED_PRESET_ID)}/validate',
                          json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    assert checked.json()['materials']['guide'] == guide_audio


def test_restore_old_candidate_removes_later_generation_settings(preset_client):
    from test_segment_versions import generated_version
    client, project = preset_client
    install(client, production_presets.PRESET_ID)
    saved = create_ready(client, project)
    assert saved.status_code == 200, saved.text
    old_video = generated_version(client, project, 'legacy', 'Old idea', 1_780_000_000)
    # Simulate a pre-explicit candidate whose snapshot had no generation block.
    document, path = backend.load_project_plan('preset-test')
    history = next(row for row in document['segments'][0]['generation_history'] if row['task_id'] == 'legacy')
    history['snapshot'].pop('generation', None)
    backend.write_plan_version(path, document)
    changed = client.patch('/api/projects/preset-test/plan/segments/S01',
                           json={'generation': {'seed': 99}, 'prompt': 'New idea'})
    assert changed.status_code == 200, changed.text
    plan = changed.json()['plan']
    assert plan['segments'][0]['generation']['seed'] == 99
    restored = client.post('/api/projects/preset-test/segments/S01/versions/legacy/restore',
                           json={'expected_plan_revision': plan['revision']})
    assert restored.status_code == 200, restored.text
    assert 'generation' not in restored.json()['segment']
    assert 'resolved_generation' not in restored.json()['segment']
    assert old_video.is_file()


def test_explicit_identity_repair_preserves_state_and_returns_same_mcp_receipt(preset_client, monkeypatch):
    from mcp import Client
    from mcp_server.server import create_server
    client, project = preset_client
    install(client, production_presets.PRESET_ID)
    original = create_ready(client, project).json()['segment']
    task = client.post('/api/projects/preset-test/tasks', json={
        'asset_id': 'S01', 'idempotency_key': 'broken',
        'pipeline_stage_id': production_presets.stage_id(production_presets.PRESET_ID)}).json()
    complete_frozen_task(task, project)
    document, path = backend.load_project_plan('preset-test')
    document['segments'][0]['workflow'] = backend.root_relative_path(Path(task['payload']['workflow']))
    document['segments'][0]['review_marker'] = 'pending_review'
    document['assembly'] = {'status': 'stale', 'output': document['segments'][0]['video']['path']}
    backend.write_plan_version(path, document)
    broken = client.get('/api/projects/preset-test/plan').json()
    assert client.get('/api/projects/preset-test/plan').json() == broken
    endpoint = '/api/projects/preset-test/segments/S01/recipe-identity/repair'
    body = {'task_id': task['id'], 'expected_revision': broken['revision']}
    dry = client.post(endpoint, json=body)
    assert dry.status_code == 200, dry.text
    assert dry.json()['repair_needed'] and not dry.json()['changed']
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as mcp:
            checked = await mcp.call_tool('repair_shot_recipe_identity', {'project_id': 'preset-test', 'asset_id': 'S01', **body})
            assert checked.structured_content['ok'], checked
            fixed = await mcp.call_tool('repair_shot_recipe_identity', {'project_id': 'preset-test', 'asset_id': 'S01', **body, 'dry_run': False})
            assert fixed.structured_content['ok'], fixed
            assert fixed.structured_content['data']['changed']
    asyncio.run(verify())
    repaired = client.get('/api/projects/preset-test/plan').json()
    expected = json.loads(json.dumps(broken))
    expected['segments'][0]['workflow'] = original['workflow']
    for key in ('revision', 'updated_at', 'recipe_identity_repairs'):
        repaired.pop(key, None)
        expected.pop(key, None)
    assert repaired == expected
    assert original['workflow'] == backend.require_project_manifest('preset-test')['production_preset']['workflow']
