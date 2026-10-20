"""Explicit resource-pack binding and published-pack first-frame tests.

The routes are exercised on an isolated FastAPI app with the real ``PrivateAuth``
middleware and the real private workspace/pack helpers. Packs are created through
the resource-pack API; projects and plans are seeded directly in the isolated
workspace so no task, model or ComfyUI is involved.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend import resource_packs
from backend.private_auth import Principal, PrivateAuth
from backend.private_sessions import AccountStore, SessionManager
from backend.private_workspaces import PrivateWorkspaces
from backend.user_context import Layout, SessionIdentity, UserContext

SERIES_A = '系列甲'
SERIES_B = '系列乙'


@pytest.fixture
def pack_env(tmp_path, monkeypatch):
    project = tmp_path / 'ComfyUI-Workspace' / 'project'
    catalog = project / 'projects'
    catalog.mkdir(parents=True)
    (catalog / 'catalog.json').write_text('{"projects": []}', encoding='utf-8')
    root = tmp_path / 'ComfyUI-Workspace' / 'private-workspaces'
    for name, value in {
        'ROOT': tmp_path / 'data-root',
        'COMFY_ROOT': tmp_path,
        'SHARED_ROOT': tmp_path / 'ComfyUI-Shared',
        'PROJECT': project,
        'PROJECTS_ROOT': catalog,
        'PROJECT_CATALOG_PATH': catalog / 'catalog.json',
        'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
        'DB_PATH': tmp_path / 'tasks.sqlite3',
        'RUNTIME': tmp_path / 'runtime',
        'INPUT_ROOT': tmp_path / 'input',
        'OUTPUT_ROOT': tmp_path / 'output',
        'PRIVATE_WORKSPACES': None,
    }.items():
        monkeypatch.setattr(backend, name, value)
    for name in ('CURRENT_PROJECT', 'CURRENT_PROJECT_ID', 'PLAN_PATH', 'MEDIA', 'TASK_TEMPLATES', 'PIPELINE_STAGES'):
        monkeypatch.setattr(backend, name, getattr(backend, name))

    layout = Layout(root, permitted_roots=(tmp_path,))
    workspaces = PrivateWorkspaces(layout)
    monkeypatch.setattr(backend, 'PRIVATE_WORKSPACES', workspaces)
    accounts = AccountStore(tmp_path / 'accounts.json', permitted_root=tmp_path)
    accounts.create_account('user001', 'one')
    accounts.create_account('user002', 'two')
    sessions = SessionManager(accounts)

    from backend.routers.resource_bindings import ROUTER as bindings_router
    from backend.routers.resource_packs import ROUTER as packs_router
    application = FastAPI()
    application.include_router(packs_router)
    application.include_router(bindings_router)
    application.add_middleware(PrivateAuth, sessions=sessions, origins=('http://testserver',),
                               authorize=lambda request, principal: None)
    owner, other = TestClient(application), TestClient(application)
    owner.headers['Authorization'] = 'Bearer ' + sessions.login('user001', 'one')
    other.headers['Authorization'] = 'Bearer ' + sessions.login('user002', 'two')
    return {'owner': owner, 'other': other, 'anonymous': TestClient(application),
            'workspaces': workspaces, 'layout': layout, 'tmp': tmp_path}


def _series_path(env, username: str, series: str) -> Path:
    return UserContext(username, env['layout']).path(Path('series') / series)


def _create_series(env, username: str, series: str) -> Path:
    path = _series_path(env, username, series)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _create_project(env, username: str, series: str, title: str) -> dict:
    principal = Principal(SessionIdentity(username, time.time() + 60), 'fixture', False)
    project_id, workspace = env['workspaces'].allocate('sample', series=series, title=title, principal=principal)
    for folder in ('plans', 'assets/image', 'assets/audio', 'assets/video'):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    hero = workspace / 'assets' / 'image' / 'hero.png'
    hero.write_bytes(b'hero-image')
    scene_image = workspace / 'assets' / 'image' / 'scene.png'
    scene_image.write_bytes(b'scene-image')
    voice = workspace / 'assets' / 'audio' / 'voice.wav'
    voice.write_bytes(b'voice-audio')
    old = workspace / 'assets' / 'image' / 'old-first.png'
    old.write_bytes(b'old-first')
    plan_path = workspace / 'plans' / 'plan.json'
    old_ref = backend.root_relative_path(old)
    plan = {
        'schema_version': 1,
        'project': project_id,
        'status': 'draft',
        'revision': 0,
        'segments': [{'id': 'S01', 'status': 'planned', 'duration_seconds': 5.0,
                      'start_seconds': 0.0, 'end_seconds': 5.0,
                      'keyframes': {'first': old_ref}}],
        'assembly': None,
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    manifest = backend.blank_project_manifest(project_id, series, title, workspace)
    manifest['plan_path'] = backend.root_relative_path(plan_path)
    manifest['media'] = {'hero.png': backend.root_relative_path(hero)}
    manifest['assets'] = [
        {'id': 'asset-hero', 'kind': 'image', 'title': '主角', 'image_path': backend.root_relative_path(hero)},
        {'id': 'asset-scene', 'kind': 'image', 'title': '场景', 'image_path': backend.root_relative_path(scene_image)},
        {'id': 'asset-voice', 'kind': 'audio', 'title': '声音', 'sources': {'A': backend.root_relative_path(voice)}},
    ]
    backend.write_json_atomic(workspace / 'director.project.json', manifest)
    backend.persist_project_manifest(manifest, make_default=False)
    return {'id': project_id, 'workspace': workspace, 'plan_path': plan_path,
            'old_first_frame': old_ref, 'manifest': manifest}


def _packs_url(series: str) -> str:
    return f'/api/series/{quote(series, safe="")}/resource-packs'


def _publish_pack(env, series: str, pack_id: str, kind: str, title: str,
                  project: dict, asset_id: str) -> dict:
    owner = env['owner']
    created = owner.post(_packs_url(series), json={'id': pack_id, 'kind': kind, 'title': title})
    assert created.status_code == 201, created.text
    added = owner.post(_packs_url(series) + f'/{pack_id}/assets',
                       json={'project_id': project['id'], 'asset_id': asset_id,
                             'role': kind, 'view': '正面'})
    assert added.status_code == 200, added.text
    published = owner.post(_packs_url(series) + f'/{pack_id}/publish')
    assert published.status_code == 200, published.text
    return published.json()['pack']


@pytest.fixture
def resources_env(pack_env):
    env = pack_env
    _create_series(env, 'user001', SERIES_A)
    _create_series(env, 'user001', SERIES_B)
    _create_series(env, 'user002', SERIES_A)
    project_a = _create_project(env, 'user001', SERIES_A, '作品甲')
    project_b = _create_project(env, 'user001', SERIES_B, '作品乙')
    project_other = _create_project(env, 'user002', SERIES_A, '用户二作品')
    characters = _publish_pack(env, SERIES_A, 'characters', 'character', '角色包', project_a, 'asset-hero')
    scenes = _publish_pack(env, SERIES_A, 'scenes', 'scene', '场景包', project_a, 'asset-scene')
    sounds = _publish_pack(env, SERIES_A, 'sounds', 'sound', '声音包', project_a, 'asset-voice')
    draft = env['owner'].post(_packs_url(SERIES_A), json={'id': 'draft-pack', 'kind': 'scene', 'title': '草稿包'})
    assert draft.status_code == 201, draft.text
    other_series = _publish_pack(env, SERIES_B, 'other-series', 'scene', '其他系列包', project_b, 'asset-scene')
    env.update({
        'project_a': project_a,
        'project_b': project_b,
        'project_other': project_other,
        'characters': characters,
        'scenes': scenes,
        'sounds': sounds,
        'other_series': other_series,
        'character_asset': characters['assets'][0]['asset_id'],
        'scene_asset': scenes['assets'][0]['asset_id'],
        'sound_asset': sounds['assets'][0]['asset_id'],
    })
    return env


def _resources_url(project: dict, segment_id: str = 'S01') -> str:
    return f'/api/projects/{project["id"]}/segments/{segment_id}/resources'


def _frame_url(project: dict, segment_id: str = 'S01') -> str:
    return f'/api/projects/{project["id"]}/segments/{segment_id}/use-resource-frame'


def _read_plan(project: dict) -> dict:
    return json.loads(Path(project['plan_path']).read_text(encoding='utf-8'))


def _segment(project: dict, segment_id: str = 'S01') -> dict:
    return next(item for item in _read_plan(project)['segments'] if item['id'] == segment_id)


def _tasks_count() -> int:
    with backend.db() as connection:
        return int(connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])


def test_get_empty_bindings_and_not_found(resources_env):
    env = resources_env
    owner = env['owner']
    response = owner.get(_resources_url(env['project_a']))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['segment_id'] == 'S01'
    assert payload['plan_revision'] == 0
    assert payload['bound'] is False
    assert payload['resources'] == {
        'schema_version': 1, 'character_pack_ids': [], 'scene_pack_id': None,
        'sound_pack_id': None, 'asset_ids': [], 'packs': [], 'bound_at': None,
    }
    assert payload['generation_submitted'] is False
    assert {item['id'] for item in payload['available_packs']} == {'characters', 'scenes', 'sounds'}
    assert all(item['version'] == 1 for item in payload['available_packs'])

    assert owner.get(_resources_url(env['project_a'], 'missing')).status_code == 404
    assert env['anonymous'].get(_resources_url(env['project_a'])).status_code == 401


def test_put_binds_published_packs_and_get_echoes(resources_env):
    env = resources_env
    owner = env['owner']
    project = env['project_a']
    before_first = _segment(project)['keyframes']['first']
    body = {
        'character_pack_ids': ['characters'],
        'scene_pack_id': 'scenes',
        'sound_pack_id': 'sounds',
        'asset_ids': [env['character_asset'], env['scene_asset']],
    }
    response = owner.put(_resources_url(project), json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    resources = payload['resources']
    assert payload['bound'] is True
    assert payload['plan_revision'] == 1
    assert resources['character_pack_ids'] == ['characters']
    assert resources['scene_pack_id'] == 'scenes'
    assert resources['sound_pack_id'] == 'sounds'
    assert resources['asset_ids'] == [env['character_asset'], env['scene_asset']]
    packs = {item['pack_id']: item for item in resources['packs']}
    assert packs['characters']['version'] == 1 and packs['characters']['kind'] == 'character'
    assert packs['characters']['asset_ids'] == [env['character_asset']]
    assert packs['scenes']['asset_ids'] == [env['scene_asset']]
    assert packs['sounds']['asset_ids'] == []
    assert all(item['version'] == 1 for item in packs.values())

    stored = _segment(project)['resources']
    assert stored == resources
    assert _segment(project)['keyframes']['first'] == before_first
    echo = owner.get(_resources_url(project)).json()
    assert echo['resources'] == resources and echo['bound'] is True
    assert echo['plan_revision'] == 1
    assert _tasks_count() == 0

    cleared = owner.put(_resources_url(project), json={
        'character_pack_ids': [], 'scene_pack_id': None, 'sound_pack_id': None, 'asset_ids': [],
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['bound'] is False
    assert cleared.json()['resources']['packs'] == []
    assert _segment(project)['resources']['packs'] == []
    assert _segment(project)['keyframes']['first'] == before_first
    assert _tasks_count() == 0

    # Omitting asset_ids records every asset of the explicitly selected pack.
    all_assets = owner.put(_resources_url(project), json={'character_pack_ids': ['characters']})
    assert all_assets.status_code == 200, all_assets.text
    assert all_assets.json()['resources']['packs'][0]['asset_ids'] == [env['character_asset']]
    assert _segment(project)['keyframes']['first'] == before_first
    assert _tasks_count() == 0


def test_binding_rejects_cross_series_unpublished_missing_and_kinds(resources_env):
    env = resources_env
    owner = env['owner']
    project_a, project_b = env['project_a'], env['project_b']

    cross_series = owner.put(_resources_url(project_b), json={
        'character_pack_ids': ['characters'], 'asset_ids': [],
    })
    assert cross_series.status_code == 404
    assert cross_series.json()['detail']['code'] == 'resource_pack_not_found'

    draft = owner.put(_resources_url(project_a), json={'character_pack_ids': ['draft-pack']})
    assert draft.status_code == 409
    assert draft.json()['detail']['code'] == 'resource_pack_not_published'

    missing = owner.put(_resources_url(project_a), json={'character_pack_ids': ['missing-pack']})
    assert missing.status_code == 404
    assert missing.json()['detail']['code'] == 'resource_pack_not_found'

    wrong_kind = owner.put(_resources_url(project_a), json={'character_pack_ids': ['scenes']})
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()['detail']['code'] == 'resource_pack_kind_mismatch'

    duplicate = owner.put(_resources_url(project_a), json={
        'character_pack_ids': ['characters', 'characters'],
    })
    assert duplicate.status_code == 422
    assert duplicate.json()['detail']['code'] == 'duplicate_pack_id'
    duplicate_field = owner.put(_resources_url(project_a), json={
        'character_pack_ids': ['characters'], 'scene_pack_id': 'characters',
    })
    assert duplicate_field.status_code == 422
    assert duplicate_field.json()['detail']['code'] == 'duplicate_pack_id'

    unknown_asset = owner.put(_resources_url(project_a), json={
        'character_pack_ids': ['characters'], 'asset_ids': ['asset-not-in-pack'],
    })
    assert unknown_asset.status_code == 422
    assert unknown_asset.json()['detail']['code'] == 'asset_not_in_selected_packs'
    unselected_asset = owner.put(_resources_url(project_a), json={
        'character_pack_ids': ['characters'], 'asset_ids': [env['sound_asset']],
    })
    assert unselected_asset.status_code == 422
    assert unselected_asset.json()['detail']['code'] == 'asset_not_in_selected_packs'
    assert _segment(project_a).get('resources') is None
    assert _segment(project_a)['keyframes']['first'] == env['project_a']['old_first_frame']
    assert _tasks_count() == 0


def test_binding_hides_other_users_project_and_pack(resources_env):
    env = resources_env
    owner, other = env['owner'], env['other']
    project_a = env['project_a']

    assert other.get(_resources_url(project_a)).status_code == 404
    assert other.put(_resources_url(project_a), json={'character_pack_ids': ['characters']}).status_code == 404
    assert env['anonymous'].put(_resources_url(project_a),
                                json={'character_pack_ids': ['characters']}).status_code == 401

    # The other account owns a same-named series, but not the owner's pack.
    own = other.put(_resources_url(env['project_other']), json={'character_pack_ids': ['characters']})
    assert own.status_code == 404
    assert own.json()['detail']['code'] == 'resource_pack_not_found'
    assert owner.get(_resources_url(project_a)).status_code == 200


def test_use_resource_frame_copies_registers_and_sets_first_frame(resources_env):
    env = resources_env
    owner = env['owner']
    project = env['project_a']
    response = owner.post(_frame_url(project), json={
        'pack_id': 'characters', 'asset_id': env['character_asset'],
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['created'] is True
    assert payload['already_current'] is False
    assert payload['target_slot'] == 'first_frame'
    assert payload['previous_first_frame'] == project['old_first_frame']
    assert payload['plan_revision'] == 1
    assert payload['pack']['pack_id'] == 'characters'
    assert payload['pack']['version'] == 1
    assert payload['frame']['asset_id'].startswith('RF-')
    segment = _segment(project)
    assert segment['keyframes']['first'] == payload['target_first_frame']
    assert segment['resource_frame']['pack_id'] == 'characters'
    assert segment['resource_frame']['pack_asset_id'] == env['character_asset']
    assert segment['resource_frame']['previous_first_frame'] == project['old_first_frame']
    frame_path = Path(payload['target_first_frame'])
    assert frame_path.is_file() and frame_path.read_bytes() == b'hero-image'
    assert frame_path.is_relative_to(project['workspace'])
    manifest = json.loads((project['workspace'] / 'director.project.json').read_text(encoding='utf-8'))
    registered = next(item for item in manifest['assets'] if item['id'] == payload['frame']['asset_id'])
    assert registered['kind'] == 'image' and registered['ready'] is True
    assert registered['image_path'] == payload['target_first_frame']
    assert registered['resource_frame']['pack_id'] == 'characters'
    assert _tasks_count() == 0

    repeated = owner.post(_frame_url(project), json={
        'pack_id': 'characters', 'asset_id': env['character_asset'],
    })
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['created'] is False
    assert repeated.json()['already_current'] is True
    assert repeated.json()['plan_revision'] == payload['plan_revision']
    assert _segment(project)['keyframes']['first'] == payload['target_first_frame']
    manifest_after = json.loads((project['workspace'] / 'director.project.json').read_text(encoding='utf-8'))
    assert len([item for item in manifest_after['assets'] if item['id'] == payload['frame']['asset_id']]) == 1


def test_use_resource_frame_rejects_audio_draft_missing_and_cross_series(resources_env):
    env = resources_env
    owner = env['owner']
    project_a, project_b = env['project_a'], env['project_b']

    audio = owner.post(_frame_url(project_a), json={'pack_id': 'sounds', 'asset_id': env['sound_asset']})
    assert audio.status_code == 422
    assert audio.json()['detail']['code'] == 'resource_asset_not_image'

    draft = owner.post(_frame_url(project_a), json={'pack_id': 'draft-pack', 'asset_id': env['character_asset']})
    assert draft.status_code == 409
    assert draft.json()['detail']['code'] == 'resource_pack_not_published'

    missing_pack = owner.post(_frame_url(project_a), json={'pack_id': 'missing', 'asset_id': 'x'})
    assert missing_pack.status_code == 404
    assert missing_pack.json()['detail']['code'] == 'resource_pack_not_found'
    missing_asset = owner.post(_frame_url(project_a), json={'pack_id': 'characters', 'asset_id': 'missing-asset'})
    assert missing_asset.status_code == 404
    assert missing_asset.json()['detail']['code'] == 'resource_asset_not_found'

    cross_series = owner.post(_frame_url(project_b), json={
        'pack_id': 'characters', 'asset_id': env['character_asset'],
    })
    assert cross_series.status_code == 404
    assert cross_series.json()['detail']['code'] == 'resource_pack_not_found'

    other = env['other']
    assert other.post(_frame_url(project_a), json={
        'pack_id': 'characters', 'asset_id': env['character_asset'],
    }).status_code == 404
    assert env['anonymous'].post(_frame_url(project_a), json={
        'pack_id': 'characters', 'asset_id': env['character_asset'],
    }).status_code == 401

    assert _segment(project_a)['keyframes']['first'] == env['project_a']['old_first_frame']
    assert _tasks_count() == 0
