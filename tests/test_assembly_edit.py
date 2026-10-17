import asyncio
import json
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import app as backend


@pytest.fixture
def edit_client(tmp_path, monkeypatch):
    project = tmp_path / 'ComfyUI-Workspace' / 'project'
    projects = project / 'projects'
    projects.mkdir(parents=True)
    (projects / 'catalog.json').write_text('{"projects": []}', encoding='utf-8')
    for name, value in {'ROOT': tmp_path, 'PROJECT': project, 'PROJECTS_ROOT': projects,
                        'PROJECT_CATALOG_PATH': projects / 'catalog.json',
                        'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
                        'DB_PATH': tmp_path / 'state.sqlite3', 'SNAPSHOT_ROOT': project / 'snapshots',
                        'INPUT_ROOT': tmp_path / 'input'}.items():
        monkeypatch.setattr(backend, name, value)
    client = TestClient(backend.app)
    created = client.post('/api/projects/create', json={'series': '测试', 'title': '接点', 'project_id': 'edit-test', 'creation_mode': 'advanced'})
    assert created.status_code == 200, created.text
    root = '/api/projects/edit-test'
    for shot_id in ('S01', 'S02'):
        response = client.post(root + '/plan/segments', json={'segment_id': shot_id, 'duration_seconds': 5})
        assert response.status_code == 200, response.text
    manifest = backend.load_project_manifest('edit-test')
    manifest['assembly']['builder'] = str(Path(__file__).resolve().parents[1] / 'tools' / 'build_assembly_workflow.py')
    backend.save_project_manifest(manifest)
    workspace = backend.resolve_registered_path(manifest['workspace_root'])
    for shot_id in ('S01', 'S02'):
        (workspace / f'{shot_id}.mp4').write_bytes(b'video fixture')
    plan_path = backend.resolve_registered_path(manifest['plan_path'])
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    for shot in plan['segments']:
        shot['video'] = {'path': str(workspace / f"{shot['id']}.mp4")}
        shot['current_version_task_id'] = f"task-{shot['id']}"
    backend.write_plan_version(plan_path, plan)
    return client, root, workspace, plan_path


def register_wav(workspace, seconds):
    path = workspace / f'master-{seconds}.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 8000 * seconds)
    manifest = backend.load_project_manifest('edit-test')
    asset_id = f'audio-{seconds}'
    manifest['assets'].append({'id': asset_id, 'kind': 'audio', 'title': path.name,
                               'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)
    return asset_id


def test_join_review_is_version_bound_and_sound_is_project_registered(edit_client):
    client, root, workspace, plan_path = edit_client
    edit = client.get(root + '/assembly/edit').json()
    assert edit['joins'][0]['status'] == 'pending'
    assert edit['joins'][0]['cut_seconds'] == 5
    assert edit['joins'][0]['left_preview_start'] == 3.5
    assert edit['blocking_joins'] == []  # untouched legacy assembly remains usable
    approved = client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': edit['revision'], 'status': 'approved',
        'constraint': '右手动作接左手方向', 'note': '接点画面和环境音已检查'})
    assert approved.status_code == 200, approved.text
    assert approved.json()['joins'][0]['status'] == 'approved'
    before = client.get(root + '/plan').json()
    conflict = client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': edit['revision'], 'status': 'redo_right'})
    assert conflict.status_code == 409
    assert client.get(root + '/plan').json() == before
    foreign = client.put(root + '/assembly/sound', json={
        'expected_revision': before['revision'], 'policy': 'complete_master', 'audio_asset_id': 'other-project-audio'})
    assert foreign.status_code == 422
    assert client.get(root + '/plan').json() == before
    short_id = register_wav(workspace, 9)
    invalid = client.put(root + '/assembly/sound', json={
        'expected_revision': before['revision'], 'policy': 'complete_master', 'audio_asset_id': short_id})
    assert invalid.status_code == 422
    assert client.get(root + '/plan').json() == before
    master_id = register_wav(workspace, 11)
    chosen = client.put(root + '/assembly/sound', json={
        'expected_revision': before['revision'], 'policy': 'complete_master', 'audio_asset_id': master_id})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()['sound']['duration_seconds'] == 11
    assert chosen.json()['sound']['audio_asset_id'] == master_id
    manifest = backend.load_project_manifest('edit-test')
    snapshot = backend.build_assembly_snapshot(manifest)
    graph = json.loads(snapshot.read_text(encoding='utf-8'))
    assert graph['concatenate']['inputs']['complete_audio'] == ['load-complete-audio', 0]
    assert graph['load-complete-audio']['inputs']['audio'].endswith('master-11.wav')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    (workspace / 'S02-next.mp4').write_bytes(b'new version')
    plan['segments'][1]['video']['path'] = str(workspace / 'S02-next.mp4')
    plan['segments'][1]['current_version_task_id'] = 'task-S02-next'
    backend.write_plan_version(plan_path, plan)
    stale = client.get(root + '/assembly/preflight').json()
    assert stale['joins'][0]['status'] == 'stale'
    assert stale['blocking_joins'] == ['S01>S02']
    assert stale['ready'] is False
    with pytest.raises(ValueError, match='接点尚未'):
        backend.build_assembly_snapshot(manifest)
    renewed = client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': stale['revision'], 'status': 'approved', 'note': '新右段已检查'})
    assert renewed.status_code == 200, renewed.text
    assert client.get(root + '/assembly/preflight').json()['ready'] is True


def test_sound_setting_can_explicitly_disable_legacy_master(edit_client):
    client, root, workspace, _ = edit_client
    register_wav(workspace, 11)
    manifest = backend.load_project_manifest('edit-test')
    manifest['assembly']['audio'] = str(workspace / 'master-11.wav')
    backend.save_project_manifest(manifest)
    current = client.get(root + '/assembly/edit').json()
    assert current['sound']['policy'] == 'legacy_master'
    changed = client.put(root + '/assembly/sound', json={
        'expected_revision': current['revision'], 'policy': 'segment_native'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['sound']['policy'] == 'segment_native'
    assert changed.json()['sound']['path'] is None
    # A saved choice overrides legacy manifest audio in the frozen graph.
    approved = client.put(root + '/assembly/joins/S01/S02', json={
        'expected_revision': changed.json()['revision'], 'status': 'approved'})
    assert approved.status_code == 200, approved.text
    graph = json.loads(backend.build_assembly_snapshot(backend.load_project_manifest('edit-test')).read_text(encoding='utf-8'))
    assert 'complete_audio' not in graph['concatenate']['inputs']


def test_preflight_checks_actual_video_file_without_rendering(edit_client):
    client, root, workspace, plan_path = edit_client
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan['segments'][1]['video']['path'] = str(workspace / 'missing.mp4')
    backend.write_plan_version(plan_path, plan)
    checked = client.get(root + '/assembly/preflight')
    assert checked.status_code == 200, checked.text
    assert checked.json()['ready'] is False
    assert checked.json()['missing_videos'] == ['S02']


def test_mcp_assembly_edit_matches_http_contract(edit_client):
    pytest.importorskip('mcp')
    from mcp import Client
    from mcp_server.server import create_server

    client, _, workspace, _ = edit_client
    master_id = register_wav(workspace, 11)

    async def verify():
        async with Client(create_server('http://localhost:4100', httpx.ASGITransport(app=backend.app))) as agent:
            tools = {tool.name for tool in (await agent.list_tools()).tools}
            assert {'read_assembly_edit', 'review_assembly_join', 'set_assembly_sound',
                    'preflight_assembly_edit'} <= tools
            edit = (await agent.call_tool('read_assembly_edit', {'project_id': 'edit-test'})).structured_content
            assert edit['ok']
            revision = edit['data']['revision']
            review = (await agent.call_tool('review_assembly_join', {
                'project_id': 'edit-test', 'left_id': 'S01', 'right_id': 'S02',
                'expected_revision': revision, 'status': 'approved', 'note': '镜头接点已检查',
            })).structured_content
            assert review['ok']
            sound = (await agent.call_tool('set_assembly_sound', {
                'project_id': 'edit-test', 'expected_revision': review['data']['revision'],
                'policy': 'complete_master', 'audio_asset_id': master_id,
            })).structured_content
            assert sound['ok']
            preflight = (await agent.call_tool('preflight_assembly_edit', {
                'project_id': 'edit-test',
            })).structured_content
            assert preflight['data']['ready'] is True
            assert preflight['data']['sound']['audio_asset_id'] == master_id

    asyncio.run(verify())
