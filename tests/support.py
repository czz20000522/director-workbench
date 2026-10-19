"""Plain test helpers shared by more than one test module.

Fixtures live in ``conftest.py``; helpers that take a client/project (or that build a
fixture file) live here so a test file no longer imports a sibling test module just to get
them.  Nothing here starts a server, a GPU job or a network connection by itself.
"""
import json
import time

import av
import numpy as np

from backend import app as backend, production_presets


def create(client):
    response = client.post('/api/projects/create', json={'title': '同名作品', 'series': '同系列', 'project_id': 'same', 'select': True, 'creation_mode': 'advanced'})
    assert response.status_code == 200, response.text
    return response.json()['project']


def install(client):
    response = client.post('/api/projects/preset-test/production-presets/h3-guided')
    assert response.status_code == 200, response.text
    return response.json()['project']


def add_shot(client, project, shot_id, with_audio=True):
    assets = project / 'workspaces' / 'preset-test' / 'assets'
    for name in (f'{shot_id}.png', f'{shot_id}.wav'):
        (assets / name).write_bytes(b'test asset')
    image = str(assets / f'{shot_id}.png')
    response = client.post('/api/projects/preset-test/plan/segments', json={'segment_id': shot_id, 'duration_seconds': 5, 'prompt': f'{shot_id} waves', 'first_frame': image, 'last_frame': image})
    assert response.status_code == 200, response.text
    if with_audio:
        assert client.patch(f'/api/projects/preset-test/plan/segments/{shot_id}', json={'audio_guide': str(assets / f'{shot_id}.wav')}).status_code == 200


def reference_image(tmp_path):
    path = tmp_path / 'reference.png'
    with av.open(str(path), 'w', format='image2') as output:
        stream = output.add_stream('png')
        stream.width, stream.height, stream.pix_fmt = 32, 48, 'rgb24'
        frame = av.VideoFrame.from_ndarray(np.zeros((48, 32, 3), dtype=np.uint8), format='rgb24')
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


def generated_version(client, project, task_id, prompt, when):
    document, plan_path = backend.load_project_plan("preset-test")
    segment = document["segments"][0]
    segment["prompt"] = prompt
    snapshot = backend.segment_generation_snapshot(segment)
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    video = project / f"workspaces/preset-test/assets/{task_id}.mp4"
    video.write_bytes(b"video " + task_id.encode())
    payload = {"project_id": "preset-test", "segment_snapshot": snapshot,
               "execution_snapshot": {"prompt": prompt, "materials": {}}}
    result = {"status": "success", "outputs": [video.name], "published_outputs": [str(video)]}
    with backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id,prompt_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "S01", "succeeded", when - 1, when, json.dumps(payload), json.dumps(result), "preset-test", task_id),
        )
        connection.commit()
    backend.record_segment_result("S01", task_id, "workflow.json", result, plan_path,
                                  task_id=task_id, payload=payload, generated_at=when)
    return video


def seed_test_models(shared_root):
    # Empty model filenames in the isolated ComfyUI shared root test availability
    # contracts only; neither real weights nor model loading is involved.
    directories = {'unet_name': 'diffusion_models', 'clip_name': 'text_encoders', 'vae_name': 'vae', 'lora_name': 'loras'}
    for node in production_presets.load_recipe().values():
        for key, directory in directories.items():
            if node['inputs'].get(key):
                path = shared_root / 'models' / directory / node['inputs'][key]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'isolated model-availability fixture')



def prepare(client, shots):
    project_id = create(client)['id']
    base = '/api/projects/' + project_id
    result = client.post(base + '/production-presets/h3-first-native-landscape')
    assert result.status_code == 200, result.text
    uploaded = client.post(base + '/upload', files=[('files', ('first.png', b'fixture-image', 'image/png'))])
    assert uploaded.status_code == 200, uploaded.text
    reference = uploaded.json()['uploaded'][0]
    for shot in shots:
        result = client.post(base + '/plan/segments', json={'segment_id': shot, 'duration_seconds': 5,
                                                          'prompt': shot + ' moves', 'first_frame': reference})
        assert result.status_code == 200, result.text
    return project_id, base


def seed_gpu_waiter(task_id='waiting-generation'):
    """An already-frozen GPU row in the isolated DB, without running any model."""
    with backend.db() as connection:
        connection.execute('INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
            (task_id, 'S01', 'scheduler_waiting', time.time(), time.time(), json.dumps({'asset_id': 'S01'}), 'project-a'))
        connection.commit()
    return task_id


def prepare_semantic(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"test")
    monkeypatch.setattr(backend, "PRIMARY_MODEL", model)
    monkeypatch.setattr(backend, "PRIMARY_MMPROJ", model)
    monkeypatch.setattr(backend, "comfy_status", lambda: {"online": False})
