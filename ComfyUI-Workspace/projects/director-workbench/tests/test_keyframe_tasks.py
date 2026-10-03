import json
import shutil
from pathlib import Path

from backend import app as backend
from test_keyframe_inputs import reference_image
from test_production_presets import preset_client


def test_text_mode_needs_no_image_and_preserves_submission_identity(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    endpoint = '/api/projects/preset-test/keyframe-tasks'
    args = {'mode': 'text', 'prompt': '清晨的小镇', 'idempotency_key': 'first-image'}
    invalid = client.post(endpoint, json={**args, 'reference_image': 'ignored.png'})
    assert invalid.status_code == 422
    assert client.post(endpoint, json={**args, 'width': 513}).status_code == 422
    assert client.post(endpoint, json={**args, 'mode': 'reference'}).status_code == 422
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
    response = client.post(endpoint, json=args)
    assert response.status_code == 202, response.text
    task = response.json()
    recipe = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert not any(node['class_type'] == 'LoadImage' for node in recipe.values())
    assert recipe['4']['inputs']['text'] == args['prompt']
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'width': 1024}).status_code == 409
    output = reference_image(project)
    backend.complete_keyframe_task(task, {'status': 'success', 'output': str(output), 'width': 32, 'height': 48})
    asset = next(a for a in client.get('/api/projects/preset-test').json()['assets'] if a.get('keyframe_task_id') == task['id'])
    assert asset['origin'] == 'Krea 2 Turbo'


def test_keyframe_submit_recover_and_register_once(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda: {'available': True})
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', project / 'output')
    reference = reference_image(project)
    args = {'prompt': '雨夜挥手', 'reference_image': str(reference), 'seed': 42, 'idempotency_key': 'image-proof'}
    endpoint = '/api/projects/preset-test/keyframe-tasks'
    before = client.get('/api/projects/preset-test/plan').json()
    response = client.post(endpoint, json=args)
    assert response.status_code == 202, response.text
    task = response.json()
    recipe = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    image_reference = recipe['17']['inputs']['image']
    assert not Path(image_reference).is_absolute()
    assert (backend.INPUT_ROOT / image_reference).read_bytes() == reference.read_bytes()
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'seed': 43}).status_code == 409
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    backend.set_task(task['id'], status='needs_reconcile', prompt_id=task['id'])
    calls = []
    history = {}
    def comfy(path, payload=None):
        calls.append(path)
        return {'queue_running': [], 'queue_pending': []} if path == '/queue' else history
    monkeypatch.setattr(backend, 'request_json', comfy)
    assert client.post(detail + '/resume').status_code == 409
    output = backend.OUTPUT_ROOT / (task['payload']['request']['output_prefix'] + '_00001_.png')
    output.parent.mkdir(parents=True)
    shutil.copy2(reference, output)
    history[task['id']] = {'status': {'status_str': 'success'}, 'outputs': {'18': {'images': [
        {'type': 'output', 'subfolder': str(output.parent.relative_to(backend.OUTPUT_ROOT)), 'filename': output.name}]}}}
    restored = client.post(detail + '/reconcile')
    assert restored.status_code == 200, restored.text
    assert restored.json()['status'] == 'succeeded'
    assert restored.json()['result']['height'] == 48
    assert client.post(detail + '/reconcile').status_code == 200
    assets = client.get('/api/projects/preset-test').json()['assets']
    assert len([a for a in assets if a.get('keyframe_task_id') == task['id']]) == 1
    candidate = next(a for a in assets if a.get('keyframe_task_id') == task['id'])
    assert candidate['image_path'] == candidate['sources']['A']
    assert client.get('/api/projects/preset-test/plan').json() == before
    assert '/prompt' not in calls
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_live_runner_registers_image_without_writing_video_plan(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda: {'available': True})
    reference = reference_image(project)
    task = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'prompt': '挥手', 'reference_image': str(reference), 'idempotency_key': 'runner'}).json()
    monkeypatch.setattr(backend, 'sample_resources', lambda _: None)
    def comfy(path, payload=None):
        if path == '/prompt':
            assert payload['prompt_id'] == task['id']
            assert payload['prompt'] == json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
            return {'prompt_id': task['id']}
        return {}
    monkeypatch.setattr(backend, 'request_json', comfy)
    monkeypatch.setattr(backend.keyframes, 'read_result', lambda *args: {'status': 'success', 'output': str(reference), 'width': 32, 'height': 48})
    backend.run_workflow_submission(task['id'], task['payload'])
    assert backend.get_task(task['id'])['status'] == 'succeeded'
    assert any(a.get('keyframe_task_id') == task['id'] for a in client.get('/api/projects/preset-test').json()['assets'])
