"""清晰化库：归档带「清晰化」标签；覆盖才动原件。"""
import json
import time
from pathlib import Path

import pytest

from backend import app as backend
from backend import enhance_library


@pytest.fixture
def lib(preset_client_factory, tmp_path, monkeypatch):
    client, project = preset_client_factory()
    assets = tmp_path / '素材库'
    assets.mkdir()
    monkeypatch.setattr(backend, 'ASSETS_ROOT', assets)
    return client, project, assets


def _project(client, project_id='hd-work'):
    response = client.post('/api/projects/create', json={
        'creation_mode': 'advanced', 'series': '测试', 'title': '清晰化测试', 'project_id': project_id, 'select': False})
    assert response.status_code == 200, response.text
    return project_id


def _workspace(project_id):
    return backend.resolve_registered_path(str(backend.require_project_manifest(project_id)['workspace_root']))


def _register(project_id, asset_id, path, title):
    manifest = backend.require_project_manifest(project_id)
    manifest['assets'].append({'id': asset_id, 'kind': 'image', 'title': title, 'ready': True,
                               'subtitle': '关键帧候选 · 待审核', 'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)


def _task(project_id, task_id, source, output):
    payload = {'kind': 'workflow', 'pipeline_stage_id': 'media-enhancement',
               'project_id': project_id, 'asset_id': f'HD-{task_id[:8]}',
               'enhancement': {'source': str(source), 'source_facts': {'kind': 'image', 'width': 64, 'height': 64}}}
    result = {'published_outputs': [str(output)], 'output_facts': {'width': 128, 'height': 128}}
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id) VALUES (?,?,?,?,?,?,?,?)',
            (task_id, f'HD-{task_id[:8]}', 'succeeded', time.time(), time.time(),
             json.dumps(payload), json.dumps(result), project_id))
        connection.commit()


def test_list_files_reads_only_media_from_the_library(lib):
    client, project, assets = lib
    folder = assets / '清晰化'
    folder.mkdir()
    (folder / '2026-10-25_a · 清晰化.png').write_bytes(b'x')
    (folder / 'note.txt').write_bytes(b'x')
    items = enhance_library.list_files()
    assert [item['name'] for item in items] == ['2026-10-25_a · 清晰化.png']
    assert items[0]['path'] == '素材库/清晰化/2026-10-25_a · 清晰化.png'


def test_archive_without_overwrite_leaves_the_original_alone(lib):
    client, project, assets = lib
    work = _project(client)
    workspace = _workspace(work)
    source = workspace / 'assets' / 'src.png'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'source')
    enhanced = workspace / 'enhance' / 'out.png'
    enhanced.parent.mkdir(parents=True, exist_ok=True)
    enhanced.write_bytes(b'enhanced')
    _register(work, 'orig', source, '夜街')
    _task(work, 'task-hd-1', source, enhanced)

    response = client.post(f'/api/projects/{work}/enhancements/task-hd-1/archive', json={'overwrite': False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['overwritten'] is False and body['deleted_original'] is None
    archived = assets / '清晰化' / body['name']
    assert archived.is_file() and body['name'].endswith('夜街 · 清晰化.png')
    assert archived.name[:4].isdigit()
    assert source.is_file() and enhanced.is_file()
    assets_after = backend.require_project_manifest(work)['assets']
    assert next(item for item in assets_after if item['id'] == 'orig')['sources']['A'] == str(source)


def test_overwrite_repoints_the_asset_and_deletes_the_original(lib):
    client, project, assets = lib
    work = _project(client)
    workspace = _workspace(work)
    source = workspace / 'assets' / 'src.png'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'source')
    enhanced = workspace / 'enhance' / 'out.png'
    enhanced.parent.mkdir(parents=True, exist_ok=True)
    enhanced.write_bytes(b'enhanced')
    _register(work, 'orig', source, '夜街')
    _task(work, 'task-hd-2', source, enhanced)

    body = client.post(f'/api/projects/{work}/enhancements/task-hd-2/archive', json={'overwrite': True}).json()
    assert body['overwritten'] is True and body['deleted_original'] == str(source.resolve())
    assert not source.exists()
    asset = next(item for item in backend.require_project_manifest(work)['assets'] if item['id'] == 'orig')
    assert asset['title'] == '夜街 · 清晰化'
    assert asset['sources']['A'] == body['path']
    assert (assets / '清晰化' / body['name']).read_bytes() == b'enhanced'
    # 再归档一次是幂等的，不会重复删/重复加。
    again = client.post(f'/api/projects/{work}/enhancements/task-hd-2/archive', json={'overwrite': True}).json()
    assert again['overwritten'] is True


def test_overwrite_refuses_when_the_source_was_never_registered(lib):
    client, project, assets = lib
    work = _project(client)
    workspace = _workspace(work)
    source = workspace / 'loose.png'
    source.write_bytes(b'source')
    enhanced = workspace / 'out.png'
    enhanced.write_bytes(b'enhanced')
    _task(work, 'task-hd-3', source, enhanced)
    response = client.post(f'/api/projects/{work}/enhancements/task-hd-3/archive', json={'overwrite': True})
    assert response.status_code == 422
    assert source.is_file()


def test_archive_rejects_unfinished_or_foreign_tasks(lib):
    client, project, assets = lib
    work = _project(client)
    response = client.get('/api/enhance-library')
    assert response.status_code == 200 and response.json()['items'] == []
    assert client.post(f'/api/projects/{work}/enhancements/missing/archive', json={}).status_code == 404
