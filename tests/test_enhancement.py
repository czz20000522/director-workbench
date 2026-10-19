"""General super-resolution contracts, with isolated CPU fixtures (no GPU)."""
import json
from pathlib import Path

import av
import numpy as np
import pytest

from backend import app as backend, enhancement
from support import reference_image


def nodes(combo=False):
    names = ['UpscaleModelLoader', 'ImageUpscaleWithModel', 'LoadImage', 'SaveImage',
             'LoadVideo', 'GetVideoComponents', 'CreateVideo', 'SaveVideo']
    info = {name: {} for name in names}
    info['UpscaleModelLoader'] = {'input': {'required': {'model_name':
        ['COMBO', {'options': [enhancement.MODEL]}] if combo else [[enhancement.MODEL]]}}}
    return info


def register_image(client, pid, tmp):
    source = reference_image(tmp)
    with source.open('rb') as handle:
        response = client.post(f'/api/projects/{pid}/upload', files={'files': ('picture.png', handle, 'image/png')})
    assert response.status_code == 200, response.text
    return response.json()['uploaded'][0]


@pytest.mark.parametrize('combo', [False, True])
def test_capability_accepts_both_comfy_schema_generations(combo):
    assert all(value['available'] for value in enhancement.capability(nodes(combo))['kinds'].values())
    assert not enhancement.capability({})['kinds']['image']['available']


def test_image_submit_freezes_input_is_idempotent_and_keeps_plan(preset_client, monkeypatch, tmp_path):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', lambda *args: nodes(True))
    queued = []
    monkeypatch.setattr(backend, 'queue_task', queued.append)
    source = register_image(client, 'preset-test', tmp_path)
    plan = client.get('/api/projects/preset-test/plan').json()
    args = {'source': source, 'idempotency_key': 'general-hd'}
    url = '/api/projects/preset-test/enhancement-tasks'
    response = client.post(url, json=args)
    assert response.status_code == 202, response.text
    task = response.json()
    assert queued == [task['id']]
    graph = json.loads(Path(task['payload']['workflow']).read_text())
    frozen = task['payload']['execution_snapshot']['staged_materials'][0]
    original = Path(frozen['source']).read_bytes()
    assert Path(frozen['path']).read_bytes() == original
    assert graph['source']['inputs']['image'] == frozen['reference']
    assert graph['upscale']['class_type'] == 'ImageUpscaleWithModel'
    assert task['payload']['enhancement']['source_facts']['kind'] == 'image'
    # Receipt recovery works even after the original source is unavailable.
    monkeypatch.setattr(enhancement, 'inspect_source', lambda _: pytest.fail('duplicate inspected input'))
    assert client.post(url, json=args).json()['id'] == task['id']
    assert client.post(url, json={**args, 'source': source + '.other'}).status_code == 409
    assert queued == [task['id']]
    assert client.get('/api/projects/preset-test/plan').json() == plan
    assert client.get('/api/projects/preset-test/enhancements').json()['tasks'][0]['id'] == task['id']


def test_unregistered_or_broken_source_and_missing_model_rejected(preset_client, monkeypatch, tmp_path):
    client, project = preset_client
    monkeypatch.setattr(backend, 'queue_task', lambda _: pytest.fail('must not queue'))
    monkeypatch.setattr(backend, 'request_json', lambda *args: {})
    url = '/api/projects/preset-test/enhancement-tasks'
    unregistered = reference_image(project / 'workspaces/preset-test/assets')
    assert client.post(url, json={'source': str(unregistered), 'idempotency_key': 'unknown'}).status_code == 422
    source = register_image(client, 'preset-test', tmp_path)
    response = client.post(url, json={'source': source, 'idempotency_key': 'missing-model'})
    assert response.status_code == 422 and '缺少' in response.text
    with backend.resolve_registered_path(source).open('wb') as handle:
        handle.write(b'not a picture')
    assert client.post(url, json={'source': source, 'idempotency_key': 'corrupt'}).status_code == 422


def tiny_video(path, frames=6):
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=24)
        stream.width, stream.height, stream.pix_fmt = 64, 64, 'yuv420p'
        for _ in range(frames):
            frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), 80, dtype=np.uint8), format='rgb24')
            for packet in stream.encode(frame): output.mux(packet)
        for packet in stream.encode(): output.mux(packet)


def test_video_uses_existing_graph_and_preserves_fps_audio_links(preset_client, monkeypatch, tmp_path):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'request_json', lambda *args: nodes())
    monkeypatch.setattr(backend, 'queue_task', lambda _: None)
    file = tmp_path / 'silent.mp4'
    tiny_video(file)
    with file.open('rb') as handle:
        uploaded = client.post('/api/projects/preset-test/upload', files={'files': ('silent.mp4', handle, 'video/mp4')}).json()['uploaded'][0]
    response = client.post('/api/projects/preset-test/enhancement-tasks', json={'source': uploaded, 'idempotency_key': 'video-hd'})
    assert response.status_code == 202, response.text
    task = response.json()
    graph = json.loads(Path(task['payload']['workflow']).read_text())
    assert graph['create-review-video']['inputs']['audio'] == ['video-components', 1]
    assert graph['create-review-video']['inputs']['fps'] == ['video-components', 2]
    assert task['payload']['enhancement']['source_facts']['frames'] == 6
    monkeypatch.setattr(enhancement, 'MAX_VIDEO_PIXELS', 64 * 64 * 3)
    with pytest.raises(ValueError, match='上限'):
        enhancement.inspect_source(file)


def test_image_history_and_result_validation_without_touching_plan(monkeypatch):
    monkeypatch.setattr(backend, 'request_json', lambda _: {'prompt': {'status': {'status_str': 'success'}, 'outputs': {
        'source': {'images': [{'filename': 'input.png', 'type': 'input'}]},
        'save': {'images': [{'filename': 'result.png', 'type': 'output', 'subfolder': 'hd'}]}}}})
    assert backend.history_done('prompt')['outputs'] == []
    assert backend.history_done('prompt', include_images=True)['outputs'] == ['hd/result.png']
    payload = {'enhancement': {'source_facts': {'kind': 'image', 'width': 64, 'height': 64}}}
    monkeypatch.setattr(backend, 'authorized_media_facts', lambda _: {'known': True, 'width': 128, 'height': 128})
    result = backend.describe_enhancement_result(payload, {'outputs': ['hd/result.png']})
    assert result['candidate_status'] == 'pending_review'
    monkeypatch.setattr(backend, 'authorized_media_facts', lambda _: {'known': True, 'width': 64, 'height': 64})
    with pytest.raises(ValueError, match='尺寸'):
        backend.describe_enhancement_result(payload, {'outputs': ['hd/result.png']})


def test_general_enhancement_requires_owned_project_and_files(private_workbench, monkeypatch, tmp_path):
    owner, other, _, _ = private_workbench
    project = owner.post('/api/projects/create', json={'title': 'HD', 'series': '测试', 'creation_mode': 'advanced'}).json()['project']
    pid = project['id']
    source = register_image(owner, pid, tmp_path)
    for suffix in ('enhancements', 'enhancement-capability'):
        assert other.get(f'/api/projects/{pid}/{suffix}').status_code in (403, 404)
    assert other.post(f'/api/projects/{pid}/enhancement-tasks', json={'source': source, 'idempotency_key': 'other'}).status_code in (403, 404)
    other_project = other.post('/api/projects/create', json={'title': 'Other', 'series': '测试', 'creation_mode': 'advanced'}).json()['project']
    assert other.post(f"/api/projects/{other_project['id']}/enhancement-tasks", json={'source': source, 'idempotency_key': 'foreign-file'}).status_code in (403, 422)


def test_uploaded_media_is_listed_verbatim_for_the_panel_selector(preset_client, tmp_path):
    """The panel selects a source by exact value, so the list must echo the upload receipt."""
    client, _ = preset_client
    source = register_image(client, 'preset-test', tmp_path)
    listed = client.get('/api/projects/preset-test/enhancements').json()['sources']
    assert source in [item['value'] for item in listed]
    assert [item['kind'] for item in listed if item['value'] == source] == ['image']


def test_worker_and_reconcile_publish_separate_candidate_without_plan_writes(preset_client, monkeypatch, tmp_path):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', tmp_path / 'output')
    monkeypatch.setattr(backend, 'request_json', lambda *args: nodes())
    monkeypatch.setattr(backend, 'queue_task', lambda _: None)
    source = register_image(client, 'preset-test', tmp_path)
    plan = client.get('/api/projects/preset-test/plan').json()
    task = client.post('/api/projects/preset-test/enhancement-tasks', json={'source': source, 'idempotency_key': 'worker'}).json()
    backend.OUTPUT_ROOT.mkdir()
    result_file = backend.OUTPUT_ROOT / 'result.png'
    result_file.write_bytes(b'CPU executor fixture; media facts mocked')

    def engine(path, data=None):
        if path == '/prompt': return {'prompt_id': task['id']}
        if path == '/queue': return {'queue_running': [], 'queue_pending': []}
        return {task['id']: {'status': {'status_str': 'success'}, 'outputs': {'save': {'images': [
            {'filename': result_file.name, 'type': 'output'}]}}}}

    monkeypatch.setattr(backend, 'request_json', engine)
    monkeypatch.setattr(backend, 'sample_resources', lambda _: None)
    monkeypatch.setattr(backend, 'authorized_media_facts', lambda _: {'known': True, 'width': 64, 'height': 96})
    backend.run_workflow_submission(task['id'], task['payload'])
    done = client.get(f"/api/projects/preset-test/tasks/{task['id']}").json()
    assert done['status'] == 'succeeded'
    assert done['result']['candidate_status'] == 'pending_review'
    backend.set_task(task['id'], status='needs_reconcile')
    reconciled = client.post(f"/api/projects/preset-test/tasks/{task['id']}/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()['status'] == 'succeeded'
    assert client.get('/api/projects/preset-test/plan').json() == plan
