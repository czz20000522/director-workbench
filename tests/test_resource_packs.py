"""Series-level reusable resource packs: storage, ownership and API contract.

The routes under ``/api/series/...`` are tested on an isolated FastAPI
application with the real ``PrivateAuth`` middleware but with an explicit
authorization callback.  The production callback in ``private_mode.install``
still needs to allow these paths; that file is outside this task's scope.
"""
from __future__ import annotations

import hashlib
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
def packs_env(tmp_path, monkeypatch):
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

    from backend.routers.resource_packs import ROUTER
    application = FastAPI()
    application.include_router(ROUTER)
    application.add_middleware(PrivateAuth, sessions=sessions, origins=('http://testserver',),
                               authorize=lambda request, principal: None)
    owner, other = TestClient(application), TestClient(application)
    owner.headers['Authorization'] = 'Bearer ' + sessions.login('user001', 'one')
    other.headers['Authorization'] = 'Bearer ' + sessions.login('user002', 'two')
    return {
        'owner': owner,
        'other': other,
        'anonymous': TestClient(application),
        'workspaces': workspaces,
        'layout': layout,
        'tmp': tmp_path,
    }


def _series_url(series: str) -> str:
    return f'/api/series/{quote(series, safe="")}/resource-packs'


def _series_path(env, username: str, series: str) -> Path:
    return UserContext(username, env['layout']).path(Path('series') / series)


def _create_series(env, username: str, series: str) -> Path:
    path = _series_path(env, username, series)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _create_project(env, username: str, series: str, title: str, *, content: bytes = b'hero-image') -> dict:
    principal = Principal(SessionIdentity(username, time.time() + 60), 'fixture', False)
    project_id, workspace = env['workspaces'].allocate('sample', series=series, title=title, principal=principal)
    for folder in ('plans', 'assets/image', 'assets/audio', 'assets/video', 'workflows/ui', 'workflows/api'):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    manifest = backend.blank_project_manifest(project_id, series, title, workspace)
    media_path = workspace / 'assets' / 'image' / 'hero.png'
    media_path.write_bytes(content)
    relative = backend.root_relative_path(media_path)
    manifest['media'] = {'hero.png': relative}
    manifest['assets'] = [{'id': 'asset-hero', 'kind': 'image', 'title': '主角', 'image_path': relative}]
    backend.write_json_atomic(workspace / 'director.project.json', manifest)
    backend.persist_project_manifest(manifest, make_default=False)
    return {'id': project_id, 'workspace': workspace, 'manifest': manifest, 'media': media_path}


def _pack_dir(env, series: str, pack_id: str) -> Path:
    return _series_path(env, 'user001', series) / resource_packs.PACK_DIRECTORY / pack_id


def test_create_list_read_and_duplicate_pack(packs_env):
    env = packs_env
    series_path = _create_series(env, 'user001', SERIES_A)
    owner = env['owner']

    created = owner.post(_series_url(SERIES_A), json={'id': 'hero-pack', 'kind': 'character', 'title': '主角设定'})
    assert created.status_code == 201, created.text
    pack = created.json()['pack']
    assert pack['id'] == 'hero-pack'
    assert pack['kind'] == 'character'
    assert pack['title'] == '主角设定'
    assert pack['status'] == 'draft'
    assert pack['version'] == 0
    assert pack['assets'] == []
    assert isinstance(pack['created_at'], (int, float))
    pack_dir = _pack_dir(env, SERIES_A, 'hero-pack')
    assert (pack_dir / resource_packs.PACK_FILE).is_file()
    assert (pack_dir / resource_packs.ASSETS_DIRECTORY).is_dir()
    assert json.loads((pack_dir / resource_packs.PACK_FILE).read_text(encoding='utf-8'))['id'] == 'hero-pack'

    duplicate = owner.post(_series_url(SERIES_A), json={'id': 'hero-pack', 'kind': 'scene', 'title': '重复'})
    assert duplicate.status_code == 409
    assert owner.post(_series_url(SERIES_A), json={'kind': 'vehicle', 'title': '非法类型'}).status_code == 422
    assert owner.post(_series_url(SERIES_A), json={'kind': 'scene', 'title': '   '}).status_code == 422
    assert owner.get(_series_url('不存在的系列')).status_code == 404

    listing = owner.get(_series_url(SERIES_A)).json()
    assert [item['id'] for item in listing['packs']] == ['hero-pack']
    assert listing['unreadable'] == []
    single = owner.get(_series_url(SERIES_A) + '/hero-pack')
    assert single.status_code == 200
    assert single.json()['pack']['id'] == 'hero-pack'
    assert owner.get(_series_url(SERIES_A) + '/missing-pack').status_code == 404


def test_add_asset_from_existing_project_media(packs_env):
    env = packs_env
    _create_series(env, 'user001', SERIES_A)
    project = _create_project(env, 'user001', SERIES_A, '作品甲')
    owner = env['owner']
    assert owner.post(_series_url(SERIES_A), json={'id': 'characters', 'kind': 'character', 'title': '角色包'}).status_code == 201

    added = owner.post(_series_url(SERIES_A) + '/characters/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-hero', 'role': '主角', 'view': '正面',
    })
    assert added.status_code == 200, added.text
    body = added.json()
    assert body['created'] is True
    asset = body['asset']
    content = project['media'].read_bytes()
    assert asset['sha256'] == hashlib.sha256(content).hexdigest()
    assert asset['size'] == len(content)
    assert asset['role'] == '主角'
    assert asset['view'] == '正面'
    assert asset['filename'] == 'hero.png'
    assert asset['media_ref'].startswith('assets/')
    copied = _pack_dir(env, SERIES_A, 'characters') / asset['media_ref']
    assert copied.read_bytes() == content
    assert len(owner.get(_series_url(SERIES_A) + '/characters').json()['pack']['assets']) == 1

    profile = project['workspace'] / 'assets' / 'image' / 'profile.png'
    profile.write_bytes(b'profile-image')
    by_reference = owner.post(_series_url(SERIES_A) + '/characters/assets', json={
        'project_id': project['id'], 'media_ref': 'assets/image/profile.png', 'role': '主角', 'view': '侧面',
    })
    assert by_reference.status_code == 200, by_reference.text
    assert by_reference.json()['created'] is True
    assert by_reference.json()['asset']['sha256'] == hashlib.sha256(b'profile-image').hexdigest()

    duplicate = owner.post(_series_url(SERIES_A) + '/characters/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-hero', 'role': '主角', 'view': '正面',
    })
    assert duplicate.status_code == 200
    assert duplicate.json()['created'] is False
    assert duplicate.json()['asset']['asset_id'] == asset['asset_id']
    assert len(owner.get(_series_url(SERIES_A) + '/characters').json()['pack']['assets']) == 2

    both = owner.post(_series_url(SERIES_A) + '/characters/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-hero', 'media_ref': 'assets/image/hero.png',
    })
    assert both.status_code == 422
    neither = owner.post(_series_url(SERIES_A) + '/characters/assets', json={'project_id': project['id']})
    assert neither.status_code == 422
    unknown = owner.post(_series_url(SERIES_A) + '/characters/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-missing',
    })
    assert unknown.status_code == 404


def test_path_traversal_and_invalid_media_refs_are_rejected(packs_env):
    env = packs_env
    series_path = _create_series(env, 'user001', SERIES_A)
    project = _create_project(env, 'user001', SERIES_A, '作品甲')
    owner = env['owner']
    assert owner.post(_series_url(SERIES_A), json={'id': 'safe-pack', 'kind': 'prop', 'title': '道具包'}).status_code == 201

    for bad_id in ('../escape', '..', 'CON', 'a b'):
        response = owner.post(_series_url(SERIES_A), json={'id': bad_id, 'kind': 'prop', 'title': '越界'})
        assert response.status_code == 422, (bad_id, response.text)
    assert not (env['tmp'] / 'escape').exists()
    assert list(env['tmp'].rglob('escape')) == []

    escaped = owner.post(_series_url(SERIES_A) + '/' + quote('../escape', safe='') + '/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-hero',
    })
    assert escaped.status_code in {404, 422}
    assert not (env['tmp'] / 'escape').exists()

    outside = env['tmp'] / 'outside.png'
    outside.write_bytes(b'outside')
    for bad_ref in ('../../outside.png', 'assets/image/../../outside.png', str(outside), '//server/share/outside.png', 'https://example.com/a.png'):
        response = owner.post(_series_url(SERIES_A) + '/safe-pack/assets', json={
            'project_id': project['id'], 'media_ref': bad_ref, 'role': '道具',
        })
        assert response.status_code == 422, (bad_ref, response.text)
    assert list(env['tmp'].rglob('outside.png')) == [outside]

    foreign_series = owner.get('/api/series/' + quote('..\\escape', safe='') + '/resource-packs')
    assert foreign_series.status_code == 422


def test_cross_user_access_is_hidden(packs_env):
    env = packs_env
    _create_series(env, 'user001', SERIES_A)
    project = _create_project(env, 'user001', SERIES_A, '作品甲')
    owner, other = env['owner'], env['other']
    assert owner.post(_series_url(SERIES_A), json={'id': 'private-pack', 'kind': 'sound', 'title': '声音包'}).status_code == 201

    assert other.get(_series_url(SERIES_A)).status_code == 404
    assert other.post(_series_url(SERIES_A), json={'id': 'other-pack', 'kind': 'sound', 'title': '越权'}).status_code == 404
    assert other.get(_series_url(SERIES_A) + '/private-pack').status_code == 404
    assert other.get(f"/api/projects/{project['id']}/resource-packs").status_code == 404
    assert other.post(_series_url(SERIES_A) + '/private-pack/assets', json={
        'project_id': project['id'], 'asset_id': 'asset-hero',
    }).status_code == 404
    assert env['anonymous'].get(_series_url(SERIES_A)).status_code == 401

    # A same-named series in the other account is a different tree and cannot see the pack.
    _create_series(env, 'user002', SERIES_A)
    assert other.get(_series_url(SERIES_A)).json()['packs'] == []
    assert other.post(_series_url(SERIES_A), json={'id': 'own-pack', 'kind': 'sound', 'title': '自己的包'}).status_code == 201
    assert other.get(_series_url(SERIES_A) + '/private-pack').status_code == 404


def test_publish_freezes_pack_and_project_listing_only_available(packs_env):
    env = packs_env
    _create_series(env, 'user001', SERIES_A)
    _create_series(env, 'user001', SERIES_B)
    project_a = _create_project(env, 'user001', SERIES_A, '作品甲')
    project_a2 = _create_project(env, 'user001', SERIES_A, '作品乙')
    project_b = _create_project(env, 'user001', SERIES_B, '作品丙')
    owner = env['owner']
    assert owner.post(_series_url(SERIES_A), json={'id': 'published-pack', 'kind': 'scene', 'title': '场景包'}).status_code == 201
    added = owner.post(_series_url(SERIES_A) + '/published-pack/assets', json={
        'project_id': project_a['id'], 'asset_id': 'asset-hero', 'role': '场景', 'view': '全景',
    })
    assert added.status_code == 200, added.text
    published = owner.post(_series_url(SERIES_A) + '/published-pack/publish')
    assert published.status_code == 200, published.text
    body = published.json()
    assert body['published'] is True
    assert body['pack']['status'] == 'published'
    assert body['pack']['version'] == 1
    frozen_assets = body['pack']['assets']
    assert len(frozen_assets) == 1

    assert owner.post(_series_url(SERIES_A) + '/published-pack/assets', json={
        'project_id': project_a2['id'], 'asset_id': 'asset-hero', 'role': '场景', 'view': '近景',
    }).status_code == 409
    repeated = owner.post(_series_url(SERIES_A) + '/published-pack/publish')
    assert repeated.status_code == 200
    assert repeated.json()['published'] is False
    assert repeated.json()['pack']['version'] == 1
    assert repeated.json()['pack']['assets'] == frozen_assets
    stored = json.loads((_pack_dir(env, SERIES_A, 'published-pack') / resource_packs.PACK_FILE).read_text(encoding='utf-8'))
    assert stored['status'] == 'published'
    assert stored['version'] == 1

    # A draft sibling and a pack from another series are not "available".
    assert owner.post(_series_url(SERIES_A), json={'id': 'draft-pack', 'kind': 'scene', 'title': '草稿包'}).status_code == 201
    assert owner.post(_series_url(SERIES_B), json={'id': 'other-pack', 'kind': 'scene', 'title': '其他系列'}).status_code == 201
    assert owner.post(_series_url(SERIES_B) + '/other-pack/publish').status_code == 200

    listing_a = owner.get(f"/api/projects/{project_a['id']}/resource-packs")
    assert listing_a.status_code == 200, listing_a.text
    payload = listing_a.json()
    assert payload['series'] == SERIES_A
    assert [item['id'] for item in payload['packs']] == ['published-pack']
    assert payload['packs'][0]['version'] == 1
    assert owner.get(f"/api/projects/{project_a2['id']}/resource-packs").json()['packs'] == [
        owner.get(_series_url(SERIES_A) + '/published-pack').json()['pack']
    ]
    assert owner.get(f"/api/projects/{project_b['id']}/resource-packs").json()['packs'][0]['id'] == 'other-pack'

    # Adding to a published pack never changes the frozen document.
    stored_after = json.loads((_pack_dir(env, SERIES_A, 'published-pack') / resource_packs.PACK_FILE).read_text(encoding='utf-8'))
    assert stored_after == stored


def test_corrupt_and_unreadable_packs_are_reported_not_hidden(packs_env):
    env = packs_env
    series_path = _create_series(env, 'user001', SERIES_A)
    owner = env['owner']
    assert owner.post(_series_url(SERIES_A), json={'id': 'good-pack', 'kind': 'prop', 'title': '道具包'}).status_code == 201
    broken = series_path / resource_packs.PACK_DIRECTORY / 'broken-pack'
    (broken / resource_packs.ASSETS_DIRECTORY).mkdir(parents=True)
    (broken / resource_packs.PACK_FILE).write_text('{not json', encoding='utf-8')

    listing = owner.get(_series_url(SERIES_A)).json()
    assert [item['id'] for item in listing['packs']] == ['good-pack']
    assert listing['unreadable'] == ['broken-pack']
    assert owner.get(_series_url(SERIES_A) + '/broken-pack').status_code == 409
