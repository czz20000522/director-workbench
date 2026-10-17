import json

from backend import app as backend
from test_production_presets import preset_client


def setup_source(client, project):
    source = backend.load_project_manifest('preset-test')
    image = project / 'workspaces/preset-test/assets/hero.png'
    image.write_bytes(b'original image')
    source['assets'] = [{'id': 'hero', 'title': '主角', 'image_path': str(image), 'review': 'approved'}]
    source['media'] = {'duplicate': str(image), 'missing': str(image.parent / 'missing.wav')}
    backend.save_project_manifest(source)
    plan_path = backend.resolve_registered_path(source['plan_path'])
    plan_path.write_text(json.dumps({'segments': [{'id': 'S01', 'keyframes': {'first': str(image)}}]}), encoding='utf-8')
    response = client.post('/api/projects/create', json={'series': '测试', 'title': '续作', 'project_id': 'sequel'})
    assert response.status_code == 200
    return image, (backend.PROJECTS_ROOT / 'preset-test.json').read_bytes(), plan_path.read_bytes()


def test_list_is_deduplicated_and_does_not_switch_project(preset_client):
    client, project = preset_client
    image, _, _ = setup_source(client, project)
    result = client.get('/api/projects/preset-test/reusable-materials').json()
    assert result['project']['title'] == '新作'
    assert len(result['materials']) == 1
    assert result['materials'][0]['path'] == str(image)
    assert client.get('/api/project').json()['id'] == 'sequel'


def test_reference_frame_is_selectable_without_adopting_it(preset_client):
    client, project = preset_client
    image = project / 'workspaces/preset-test/assets/reference.png'
    image.write_bytes(b'reference image')
    manifest = backend.load_project_manifest('preset-test')
    plan_path = backend.resolve_registered_path(manifest['plan_path'])
    plan_path.write_text(json.dumps({'segments': [{
        'id': 'REF-01', 'reference_frame': str(image),
        'keyframes': {'first': None, 'last': None}, 'source_analysis': 'reference',
    }]}), encoding='utf-8')
    original = plan_path.read_bytes()
    response = client.get('/api/projects/preset-test/reusable-materials')
    assert response.status_code == 200
    item, = response.json()['materials']
    assert item['path'] == str(image)
    assert item['kind'] == 'image'
    assert '参考画面' in item['title']
    assert plan_path.read_bytes() == original


def test_copy_is_independent_idempotent_and_keeps_source_and_reviews(preset_client):
    client, project = preset_client
    image, manifest_before, plan_before = setup_source(client, project)
    target = backend.load_project_manifest('sequel')
    target['assets'] = [{'id': 'existing', 'title': 'existing', 'review': 'approved'}]
    backend.save_project_manifest(target)
    item = client.get('/api/projects/preset-test/reusable-materials').json()['materials'][0]
    body = {'source_project_id': 'preset-test', 'material_ids': [item['id'], item['id']]}
    result = client.post('/api/projects/sequel/materials/reuse', json=body)
    assert result.status_code == 200, result.text
    record = result.json()['registered'][0]
    assert 'review' not in record
    copy = backend.resolve_registered_path(record['image_path'])
    assert copy != image and copy.read_bytes() == image.read_bytes()
    again = client.post('/api/projects/sequel/materials/reuse', json=body).json()
    assert again == {'registered': [], 'already_registered': 1}
    assert backend.load_project_manifest('sequel')['assets'][0] == target['assets'][0]
    assert (backend.PROJECTS_ROOT / 'preset-test.json').read_bytes() == manifest_before
    assert backend.resolve_registered_path(backend.load_project_manifest('preset-test')['plan_path']).read_bytes() == plan_before
    image.write_bytes(b'new source version')
    assert copy.read_bytes() == b'original image'
    assert client.post('/api/projects/sequel/materials/reuse', json=body).status_code == 409
    updated = client.get('/api/projects/preset-test/reusable-materials').json()['materials'][0]
    assert updated['id'] != item['id']
    response = client.post('/api/projects/sequel/materials/reuse', json={'source_project_id': 'preset-test', 'material_ids': [updated['id']]})
    assert len(response.json()['registered']) == 1
    assert copy.read_bytes() == b'original image'


def test_unlisted_and_missing_sources_rejected_before_copy(preset_client):
    client, project = preset_client
    setup_source(client, project)
    item = client.get('/api/projects/preset-test/reusable-materials').json()['materials'][0]
    response = client.post('/api/projects/sequel/materials/reuse', json={'source_project_id': 'preset-test', 'material_ids': [item['id'], '../secret']})
    assert response.status_code == 409
    assert not (project / 'workspaces/sequel/assets/reused').exists()
    assert client.get('/api/projects/unknown/reusable-materials').status_code == 404
    assert client.post('/api/projects/preset-test/materials/reuse', json={'source_project_id': 'preset-test', 'material_ids': [item['id']]}).status_code == 422


def test_plan_audio_video_and_outside_paths(preset_client, tmp_path):
    client, project = preset_client
    setup_source(client, project)
    source = backend.load_project_manifest('preset-test')
    audio = project / 'workspaces/preset-test/assets/voice.wav'
    video = project / 'workspaces/preset-test/assets/clip.mp4'
    outside = tmp_path / 'outside.png'
    for path in (audio, video, outside):
        path.write_bytes(b'test media')
    source['media']['outside'] = str(outside)
    backend.save_project_manifest(source)
    plan_path = backend.resolve_registered_path(source['plan_path'])
    plan_path.write_text(json.dumps({'segments': [{'id': 'S02', 'audio': {'guide': str(audio)}, 'video': {'path': str(video)}}]}), encoding='utf-8')
    materials = client.get('/api/projects/preset-test/reusable-materials').json()['materials']
    assert {item['kind'] for item in materials} == {'image', 'audio', 'video'}
    assert all(item['path'] != str(outside) for item in materials)
    result = client.post('/api/projects/sequel/materials/reuse', json={'source_project_id': 'preset-test', 'material_ids': [item['id'] for item in materials]})
    assert result.status_code == 200, result.text
    copied = result.json()['registered']
    assert len(copied) == 3
    for item in copied:
        path = item.get('image_path') or item['sources']['A']
        assert backend.resolve_registered_path(path).is_file()


def test_source_change_during_copy_does_not_register_partial_batch(preset_client, monkeypatch):
    client, project = preset_client
    image, _, _ = setup_source(client, project)
    item = client.get('/api/projects/preset-test/reusable-materials').json()['materials'][0]
    real_copy = backend.shutil.copy2

    def change_source(source, destination):
        real_copy(source, destination)
        image.write_bytes(b'changed while copying')

    monkeypatch.setattr(backend.shutil, 'copy2', change_source)
    response = client.post('/api/projects/sequel/materials/reuse', json={'source_project_id': 'preset-test', 'material_ids': [item['id']]})
    assert response.status_code == 409
    assert backend.load_project_manifest('sequel')['assets'] == []
