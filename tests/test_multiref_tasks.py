"""H3 multi-reference workbench task tests.

The HTTP/task contract is exercised against the isolated private project
fixture. ComfyUI and the scheduler are deliberately faked: no model is loaded,
no prompt is submitted and no service is started. The same task record is then
passed through the real ``run_workflow_submission`` with fake ComfyUI receipts
to prove the existing segment-result registration path is used.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import multiref_recipe as recipe
from backend import multiref_tasks
from backend.multiref_tasks import MultiRefTaskRequest
from backend.routers.multiref_tasks import ROUTER

PROJECT_ID = 'preset-test'
BASE = f'/api/projects/{PROJECT_ID}'
TASKS_PATH = '/api/projects/{project_id}/multiref-tasks'
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'fixture-image-body'


@pytest.fixture
def multiref_client(preset_client, monkeypatch):
    client, project = preset_client
    paths = set(backend.app.openapi().get('paths', {}))
    if TASKS_PATH not in paths:
        backend.app.include_router(ROUTER)
        # ``preset_client`` already mounted the static shell at the end of the
        # route list; move the included router before that catch-all mount.
        added = backend.app.routes.pop()
        mount_index = next((index for index, route in enumerate(backend.app.routes)
                            if getattr(route, 'path', None) in ('/', '')), len(backend.app.routes))
        backend.app.router.routes.insert(mount_index, added)
    monkeypatch.setattr(backend, 'queue_task', lambda task_id: None)
    monkeypatch.setattr(backend, 'sample_resources', lambda task_id: None)

    response = client.post(f'{BASE}/upload', files=[
        ('files', ('ref-a.png', PNG_BYTES, 'image/png')),
        ('files', ('ref-b.png', PNG_BYTES, 'image/png')),
        ('files', ('scene.png', PNG_BYTES, 'image/png')),
    ])
    assert response.status_code == 200, response.text
    manifest = backend.load_project_manifest(PROJECT_ID)
    assets = [asset for asset in manifest.get('assets', []) if asset.get('kind') == 'image']
    by_name = {Path(str(asset.get('image_path') or '')).name: asset['id'] for asset in assets}
    document, plan_path = backend.load_project_plan(PROJECT_ID)
    document.update({
        'revision': 1,
        'segments': [{
            'id': 'S01', 'start_seconds': 0, 'end_seconds': 5,
            'duration_seconds': 5, 'status': 'planned', 'prompt': '旧分镜提示',
        }],
        'assembly': None,
    })
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    return client, project, by_name


def _fake_prepare(request, *, object_info=None, base_url=recipe.COMFY_URL):
    metadata = recipe.validate_request(request)
    return {
        'prompt': recipe.build_graph(request),
        'metadata': metadata,
        'environment': {'ready': True, 'blockers': []},
    }


def _post(client, **overrides):
    payload = {
        'segment_id': 'S01',
        'reference_asset_ids': ['ref-a', 'ref-b'],
        'scene_asset_id': 'scene',
        'prompt': '角色站在窗边，镜头缓慢推近',
        'duration_seconds': 5,
        'aspect_ratio': '16:9',
        'seed': 42,
    }
    payload.update(overrides)
    return client.post(f'{BASE}/multiref-tasks', json=payload)


def _with_assets(client, project, by_name, **overrides):
    payload = {
        'reference_asset_ids': [by_name['ref-a.png'], by_name['ref-b.png']],
        'scene_asset_id': by_name['scene.png'],
    }
    payload.update(overrides)
    return _post(client, **payload)


def test_create_task_copies_inputs_writes_workflow_and_queues_existing_task(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)

    response = _with_assets(client, project, assets)
    assert response.status_code == 202, response.text
    task = response.json()
    assert task['project_id'] == PROJECT_ID
    assert task['asset_id'] == 'S01'
    assert task['status'] == 'scheduler_waiting'
    assert task['payload']['kind'] == 'workflow'
    assert task['payload']['multiref']['reference_asset_ids'] == [assets['ref-a.png'], assets['ref-b.png']]
    assert task['payload']['multiref']['scene_asset_id'] == assets['scene.png']

    staged = task['payload']['execution_snapshot']['staged_materials']
    assert len(staged) == 3
    assert all(item['input'] == 'image' for item in staged)
    assert all(Path(item['path']).is_file() for item in staged)
    assert all(Path(item['path']).name.startswith('dsh-multiref-') for item in staged)
    assert all(item['size_bytes'] == len(PNG_BYTES) for item in staged)
    assert all(Path(item['path']).parent == Path(backend.INPUT_ROOT) for item in staged)

    workflow = Path(task['payload']['workflow'])
    assert workflow.is_file()
    graph = json.loads(workflow.read_text(encoding='utf-8'))
    assert graph['i2v']['inputs']['first_frame'] == ['still_decode', 0]
    assert graph['still']['inputs']['edit_image'] == ['load_primary', 0]
    assert graph['still']['inputs']['ref_images.ref_image_0'] == ['load_ref_0', 0]
    assert graph['still']['inputs']['ref_images.ref_image_1'] == ['load_ref_1', 0]
    assert task['payload']['segment_snapshot']['prompt'] == '角色站在窗边，镜头缓慢推近'

    got = client.get(f'{BASE}/multiref-tasks/{task["id"]}')
    assert got.status_code == 200, got.text
    assert got.json()['id'] == task['id']
    assert got.json()['payload']['multiref']['recipe'] == recipe.RECIPE_ID


def test_successful_runner_registers_segment_video_candidate(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    task = _with_assets(client, project, assets).json()
    output = project / 'workspaces' / PROJECT_ID / 'assets' / 'multiref-out.mp4'
    output.write_bytes(b'fake-video')
    done = {'status': 'success', 'outputs': ['multiref-out.mp4'],
            'published_outputs': [str(output)], 'prompt_id': 'comfy-multiref-1'}

    def fake_request(path, payload=None, **kwargs):
        if path == '/prompt':
            return {'prompt_id': 'comfy-multiref-1', 'number': 1, 'node_errors': {}}
        raise AssertionError(f'unexpected request {path}')

    monkeypatch.setattr(backend, 'request_json', fake_request)
    monkeypatch.setattr(backend, 'history_done', lambda prompt_id, include_images=False: done)
    monkeypatch.setattr(backend, 'publish_task_outputs', lambda current, value: value)

    backend.run_workflow_submission(task['id'], task['payload'])
    current = backend.get_task(task['id'])
    assert current['status'] == 'succeeded', current
    document = json.loads(Path(task['payload']['plan_path']).read_text(encoding='utf-8'))
    segment = next(item for item in document['segments'] if item['id'] == 'S01')
    assert segment['status'] == 'video_generated'
    assert segment['video']['path'] == str(output)
    assert segment['current_version_task_id'] == task['id']
    state = client.get(f'{BASE}/state').json()
    assert 'S01' not in (state.get('reviews') or {})


def test_unknown_runner_receipt_becomes_needs_reconcile(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    task = _with_assets(client, project, assets).json()

    def failing_request(path, payload=None, **kwargs):
        raise RuntimeError('simulated unknown prompt receipt')

    monkeypatch.setattr(backend, 'request_json', failing_request)
    backend.run_workflow_submission(task['id'], task['payload'])
    current = backend.get_task(task['id'])
    assert current['status'] == 'needs_reconcile', current
    assert 'simulated unknown prompt receipt' in str(current.get('error'))


def test_single_reference_without_scene_uses_character_as_picture_one(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    response = _with_assets(client, project, assets, scene_asset_id=None,
                            reference_asset_ids=[assets['ref-a.png']])
    assert response.status_code == 202, response.text
    task = response.json()
    assert task['payload']['multiref']['scene_asset_id'] is None
    staged = task['payload']['execution_snapshot']['staged_materials']
    assert len(staged) == 1
    assert '-primary' in staged[0]['reference']
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['still']['inputs']['edit_image'] == ['load_primary', 0]
    assert not [key for key in graph['still']['inputs'] if key.startswith('ref_images.')]


def test_two_references_without_scene_preserve_character_order(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    response = _with_assets(client, project, assets, scene_asset_id=None,
                            reference_asset_ids=[assets['ref-b.png'], assets['ref-a.png']])
    assert response.status_code == 202, response.text
    task = response.json()
    assert len(task['payload']['execution_snapshot']['staged_materials']) == 2
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['load_primary']['inputs']['image'].endswith('-primary.png')
    assert graph['load_ref_0']['inputs']['image'].endswith('-ref-02.png')


def test_no_reference_assets_is_a_structured_blocker(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    before = len(list(Path(backend.INPUT_ROOT).glob('dsh-multiref-*')))
    response = _with_assets(client, project, assets, reference_asset_ids=[])
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'reference_images_required'
    assert len(list(Path(backend.INPUT_ROOT).glob('dsh-multiref-*'))) == before


def test_cross_project_asset_is_rejected(multiref_client, monkeypatch):
    client, project, assets = multiref_client
    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', _fake_prepare)
    created = client.post('/api/projects/create', json={
        'series': 'other', 'title': 'other', 'project_id': 'other-project',
        'creation_mode': 'advanced',
    })
    assert created.status_code == 200, created.text
    uploaded = client.post('/api/projects/other-project/upload', files=[
        ('files', ('foreign.png', PNG_BYTES, 'image/png')),
    ])
    assert uploaded.status_code == 200, uploaded.text
    foreign_manifest = backend.load_project_manifest('other-project')
    foreign_id = next(asset['id'] for asset in foreign_manifest['assets']
                      if asset.get('kind') == 'image')

    response = _with_assets(client, project, assets, reference_asset_ids=[foreign_id])
    assert response.status_code == 403, response.text
    assert response.json()['detail']['code'] == 'reference_asset_not_owned'


def test_missing_environment_node_fails_before_task_insertion(multiref_client, monkeypatch):
    client, project, assets = multiref_client

    def missing_node(request, *, object_info=None, base_url=recipe.COMFY_URL):
        raise recipe.MultiRefError(
            'node_missing', 'ComfyUI 缺少节点 MiniMaxH3StillConditioningT8。', 503,
            blockers=[{'code': 'node_missing', 'node': 'MiniMaxH3StillConditioningT8'}],
        )

    monkeypatch.setattr(multiref_tasks.recipe, 'prepare', missing_node)
    response = _with_assets(client, project, assets)
    assert response.status_code == 503, response.text
    assert response.json()['detail']['code'] == 'node_missing'
    with backend.db() as connection:
        count = connection.execute('SELECT COUNT(*) FROM tasks WHERE project_id=?', (PROJECT_ID,)).fetchone()[0]
    assert count == 0
    assert not list(Path(backend.INPUT_ROOT).glob('dsh-multiref-*'))


def test_validation_happens_before_copy_for_bad_segment(multiref_client):
    client, project, assets = multiref_client
    response = _with_assets(client, project, assets, segment_id='S99')
    assert response.status_code == 404, response.text
    assert response.json()['detail']['code'] == 'segment_not_found'
