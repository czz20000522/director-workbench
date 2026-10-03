import json
from pathlib import Path

from backend import app as backend
from test_production_presets import preset_client, add_shot


def test_assembly_uses_target_plan_current_video_without_review(preset_client, monkeypatch):
    client, project = preset_client
    add_shot(client, project, 'S01')
    builder = project / 'tools' / 'target_builder.py'
    builder.parent.mkdir(exist_ok=True)
    builder.write_text('def build_graph(plan, audio, prefix):\n    return {"1": {"class_type": "TestOutput", "inputs": {"text": plan["segments"][0]["prompt"], "filename_prefix": prefix}}}\n', encoding='utf-8')
    manifest = backend.load_project_manifest('preset-test')
    assert manifest['assembly_asset_id'] == 'MASTER'
    assert manifest['assembly']['filename_prefix'] == '导演工作台工作目录/preset-test/assembly'
    manifest['assembly'] = {'builder': 'tools/target_builder.py', 'filename_prefix': 'target/assembly', 'audio': str(project / 'workspaces/preset-test/assets/S01.wav')}
    backend.save_project_manifest(manifest)
    client.post('/api/projects/create', json={'series': '测试', 'title': 'second', 'project_id': 'second'})
    url = '/api/projects/preset-test/assembly'
    assert client.post(url).status_code == 409
    plan, plan_path = backend.load_project_plan('preset-test')
    video = project / 'workspaces/preset-test/assets/S01.mp4'
    video.write_bytes(b'current video fixture')
    plan['segments'][0]['video'] = {'path': str(video)}
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    revision = client.get('/api/projects/preset-test/plan').json()['revision']
    args = {'idempotency_key': 'assembly-proof', 'expected_revision': revision}
    assert client.post(url, json={**args, 'expected_revision': revision - 1}).status_code == 409
    response = client.post(url, json=args)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['asset_id'] == manifest['assembly_asset_id']
    assert task['project_id'] == 'preset-test'
    assert task['payload']['plan_path'] == str(backend.resolve_registered_path(manifest['plan_path']))
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['1']['inputs']['text'] == 'S01 waves'
    assert graph['1']['inputs']['filename_prefix'] == 'target/assembly'
    assert client.get('/api/project').json()['id'] == 'second'
    assert client.get('/api/projects/second/tasks').json()['tasks'] == []
    backend.set_task(task['id'], status='succeeded')
    def offline():
        raise RuntimeError('must return receipt before checking runtime')
    monkeypatch.setattr(backend, 'require_submission_capacity', offline)
    assert client.post(url, json=args).json()['id'] == task['id']
    assert client.post(url, json={**args, 'expected_revision': revision + 1}).status_code == 409
    assert client.get('/api/projects/preset-test/submission-receipt', params={'key': 'assembly-proof'}).json()['id'] == task['id']
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_empty_and_unknown_project_cannot_assemble(preset_client):
    client, _ = preset_client
    assert client.post('/api/projects/preset-test/assembly').status_code == 422
    assert client.post('/api/projects/missing/assembly').status_code == 404


def test_shot_id_cannot_collide_with_assembly_asset(preset_client):
    client, _ = preset_client
    created = client.post('/api/projects/preset-test/plan/segments', json={
        'segment_id': 'MASTER', 'duration_seconds': 5, 'expected_revision': 0})
    assert created.status_code == 200, created.text
    assert created.json()['segment']['id'] == 'MASTER-2'


def test_new_project_does_not_inherit_builder_demo_audio(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01')
    plan, plan_path = backend.load_project_plan('preset-test')
    video = project / 'workspaces/preset-test/assets/S01.mp4'
    video.write_bytes(b'current video fixture')
    plan['segments'][0]['video'] = {'path': str(video)}
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    builder = project / 'tools' / 'audio_builder.py'
    builder.parent.mkdir(exist_ok=True)
    builder.write_text('DEFAULT_AUDIO = "unrelated-demo.wav"\ndef build_graph(plan, audio, prefix):\n    return {"audio": audio}\n', encoding='utf-8')
    manifest = backend.load_project_manifest('preset-test')
    manifest['assembly'] = {'builder': 'tools/audio_builder.py'}
    backend.save_project_manifest(manifest)
    snapshot = backend.build_assembly_snapshot(manifest)
    assert json.loads(snapshot.read_text(encoding='utf-8'))['audio'] == ''
