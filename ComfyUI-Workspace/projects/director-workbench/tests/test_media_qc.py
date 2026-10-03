"""QC artifacts stay readable without becoming creative assets; no GPU dispatch."""
import json
import subprocess
import threading
from pathlib import Path

import pytest

from backend import app as backend, media_operations
from test_keyframe_inputs import reference_image
from test_production_presets import preset_client as base_client

ROOT = '/api/projects/preset-test'
REAL_THREAD = threading.Thread


@pytest.fixture
def client(base_client, monkeypatch):
    # The inherited fixture blocks generation. CPU workers still need real pipe readers.
    monkeypatch.setattr(backend.threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'queue_task', lambda task_id: None)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('QC contacted ComfyUI'))
    monkeypatch.setattr(backend.media_operations, 'capability', lambda: {'operations': {
        name: {'available': True} for name in ('video_qc', 'silence')
    }})
    return base_client


def submit(client, **body):
    response = client.post(ROOT + '/media-tasks', json=body)
    assert response.status_code == 202, response.text
    return response.json()


def execute(task):
    backend.set_task(task['id'], status='preparing')
    backend.run_media_operation_task(task['id'], task['payload'])
    completed = backend.get_task(task['id'])
    assert completed['status'] == 'succeeded', completed.get('error')
    return completed


def test_real_qc_receipt_preserves_previews_without_registering_creative_assets(client):
    api, project = client
    source = project / 'source.mp4'
    subprocess.run([
        str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-n',
        '-f', 'lavfi', '-i', 'testsrc=size=64x48:rate=12:duration=0.5',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source),
    ], check=True, timeout=30, capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    original_source = source.read_bytes()
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': 'source', 'title': 'Test video', 'kind': 'video',
                               'sources': {'A': str(source)}})
    backend.save_project_manifest(manifest)
    before = api.get(ROOT).json()['assets']
    before_plan = api.get(ROOT + '/plan').json()
    body = {'operation': 'video_qc', 'source_asset_id': 'source', 'sample_count': 3,
            'idempotency_key': 'qc-diagnostic-proof'}
    task = submit(api, **body)
    completed = execute(task)
    outputs = completed['result']['outputs']
    assert len([item for item in outputs if item['kind'] == 'image']) == 4
    assert all(item['purpose'] == 'diagnostic' and 'asset_id' not in item for item in outputs)
    assert completed['result']['report']['frame_count'] == 6
    assert api.get(ROOT).json()['assets'] == before
    assert api.get(ROOT + '/plan').json() == before_plan
    for item in outputs:
        path = Path(item['path'])
        assert path.is_file() and path.stat().st_size == item['bytes']
        preview = api.get('/media-file', params={'path': item['path']})
        assert preview.status_code == 200, preview.text
        assert preview.content == path.read_bytes()
    # Recovery and a repeat request retain the original results without new assets.
    backend.complete_media_operation_task(completed, media_operations.read_receipt(task['payload']))
    assert api.post(ROOT + '/media-tasks', json=body).json()['id'] == task['id']
    assert api.get(ROOT + '/tasks/' + task['id']).json()['result']['outputs'] == outputs
    assert api.get(ROOT).json()['assets'] == before
    assert source.read_bytes() == original_source
    # Shared browser fixture contains only isolated test data and real CPU JPEGs.
    image = next(item['path'] for item in outputs if item['kind'] == 'image')
    ordinary = reference_image(project)
    (project / 'media-qc-browser.json').write_text(json.dumps({
        'task': completed,
        'project': {'assets': [
            {'id': 'ordinary', 'title': 'Ordinary image', 'kind': 'image', 'image_path': str(ordinary),
             'sources': {'A': str(ordinary)}},
            {'id': 'legacy-qc', 'title': 'Legacy QC', 'kind': 'image', 'operation': 'video_qc',
             'image_path': image, 'sources': {'A': image}},
            {'id': 'diagnostic', 'title': 'Diagnostic purpose', 'kind': 'image', 'purpose': 'diagnostic',
             'sources': {'A': outputs[1]['path']}},
        ], 'media': {'QC alias': image.replace('\\', '/').upper()}},
        'segments': [{'id': 'S01', 'keyframes': {'first': image, 'last': str(ordinary)}}],
        'ordinary_path': str(ordinary), 'diagnostic_path': image,
    }, ensure_ascii=False), encoding='utf-8')


def test_real_non_qc_audio_still_registers_one_creative_candidate(client):
    api, _ = client
    before = api.get(ROOT + '/plan').json()
    task = submit(api, operation='silence', duration_seconds=.1, idempotency_key='normal-audio')
    completed = execute(task)
    output = next(item for item in completed['result']['outputs'] if item['kind'] == 'audio')
    assert 'purpose' not in output
    assets = api.get(ROOT).json()['assets']
    assert len(assets) == 1 and assets[0]['kind'] == 'audio'
    assert assets[0]['id'] == output['asset_id'] and assets[0]['operation'] == 'silence'
    assert api.get('/media-file', params={'path': output['path']}).status_code == 200
    assert api.get(ROOT + '/plan').json() == before
    backend.complete_media_operation_task(completed, media_operations.read_receipt(task['payload']))
    assert api.get(ROOT).json()['assets'] == assets


def test_normal_keyframe_completion_still_registers_a_creative_image(client, monkeypatch):
    api, project = client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    response = api.post(ROOT + '/keyframe-tasks', json={
        'mode': 'text', 'prompt': 'Synthetic test image', 'idempotency_key': 'normal-keyframe',
    })
    assert response.status_code == 202, response.text
    task = response.json()
    image = reference_image(project)
    # Complete the existing GPU task contract with a CPU fixture; never execute its graph.
    backend.complete_keyframe_task(task, {'status': 'success', 'output': str(image), 'width': 32, 'height': 48})
    asset = next(item for item in api.get(ROOT).json()['assets'] if item.get('keyframe_task_id') == task['id'])
    assert asset['kind'] == 'image' and asset['ready'] is True
    assert asset.get('purpose') != 'diagnostic' and asset.get('operation') != 'video_qc'
    assert asset['sources']['A']


@pytest.mark.parametrize('diagnostic_flag', [{'operation': 'video_qc'}, {'purpose': 'diagnostic'}])
def test_legacy_qc_cannot_be_reused_through_media_or_segment_aliases(client, diagnostic_flag):
    api, project = client
    ordinary = reference_image(project)
    diagnostic = ordinary.with_name('legacy-qc.png')
    diagnostic.write_bytes(ordinary.read_bytes())
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'] = [
        {'id': 'ordinary', 'kind': 'image', 'image_path': str(ordinary)},
        {'id': 'legacy-qc', 'kind': 'image', 'image_path': str(diagnostic)},
    ]
    backend.save_project_manifest(manifest)
    old_id = next(item['id'] for item in api.get(ROOT + '/reusable-materials').json()['materials']
                  if Path(item['path']) == diagnostic)
    # Reproduce the legacy registration, including alternate slash/case aliases.
    manifest['assets'][1].update(diagnostic_flag)
    manifest['media'] = {'QC alias': str(diagnostic).replace('\\', '/').upper()}
    backend.save_project_manifest(manifest)
    plan, plan_path = backend.load_project_plan('preset-test')
    plan['segments'] = [{'id': 'S01', 'reference_frame': str(diagnostic),
                         'keyframes': {'first': str(diagnostic), 'last': str(ordinary)}}]
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    manifest_path = backend.PROJECTS_ROOT / 'preset-test.json'
    manifest_before, plan_before = manifest_path.read_bytes(), plan_path.read_bytes()
    materials = api.get(ROOT + '/reusable-materials').json()['materials']
    assert len(materials) == 1 and Path(materials[0]['path']) == ordinary
    assert api.post('/api/projects/create', json={
        'series': 'Test', 'title': 'Independent target', 'project_id': 'sequel',
        'creation_mode': 'advanced',
    }).status_code == 200
    response = api.post('/api/projects/sequel/materials/reuse', json={
        'source_project_id': 'preset-test', 'material_ids': [old_id],
    })
    assert response.status_code == 409
    target = backend.require_project_manifest('sequel')
    assert target['assets'] == []
    assert not (backend.resolve_registered_path(target['workspace_root']) / 'assets/reused').exists()
    response = api.post('/api/projects/sequel/materials/reuse', json={
        'source_project_id': 'preset-test', 'material_ids': [materials[0]['id']],
    })
    assert response.status_code == 200, response.text
    copied = response.json()['registered'][0]
    assert copied['kind'] == 'image'
    assert backend.resolve_registered_path(copied['image_path']).read_bytes() == ordinary.read_bytes()
    assert manifest_path.read_bytes() == manifest_before
    assert plan_path.read_bytes() == plan_before
