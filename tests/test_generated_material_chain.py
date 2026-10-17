"""Candidate registration through shot inputs and frozen H3 submission; no GPU."""
import json
import wave
from pathlib import Path

from backend import app as backend
from test_keyframe_inputs import reference_image
from test_production_presets import install, preset_client


def test_generated_candidates_feed_shot_without_manual_workflow(preset_client, monkeypatch):
    client, project = preset_client
    install(client)
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True})
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    image = reference_image(project)
    voice = project / 'voice.wav'
    with wave.open(str(voice), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\0\0' * 24000)

    keyframe = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'mode': 'text', 'prompt': '小镇里的水豚', 'idempotency_key': 'chain-image'})
    assert keyframe.status_code == 202, keyframe.text
    backend.complete_keyframe_task(keyframe.json(), {
        'status': 'success', 'output': str(image), 'width': 32, 'height': 48})
    speech = client.post('/api/projects/preset-test/speech-tasks', json={
        'text': '你好', 'voice_reference': str(voice), 'idempotency_key': 'chain-voice'})
    assert speech.status_code == 202, speech.text
    backend.complete_speech_task(speech.json(), {
        'status': 'succeeded', 'output': str(voice), 'duration_seconds': 1,
        'sample_rate': 24000, 'channels': 1})

    # Bind only references returned by the manifest, as the material selector does.
    assets = client.get('/api/projects/preset-test').json()['assets']
    frame_ref = next(a['sources']['A'] for a in assets if a.get('keyframe_task_id') == keyframe.json()['id'])
    audio_ref = next(a['sources']['A'] for a in assets if a.get('speech_task_id') == speech.json()['id'])
    plan = client.get('/api/projects/preset-test/plan').json()
    created = client.post('/api/projects/preset-test/plan/segments', json={
        'expected_revision': plan.get('revision', 0), 'segment_id': 'S01', 'duration_seconds': 5,
        'prompt': '水豚抬手问好', 'first_frame': frame_ref, 'last_frame': frame_ref,
        'audio_guide': audio_ref})
    assert created.status_code == 200, created.text
    checked = client.post('/api/projects/preset-test/pipeline/motion-generation/validate',
                          json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    task = client.post('/api/tasks', json={'asset_id': 'S01', 'idempotency_key': 'chain-video'})
    assert task.status_code == 200, task.text
    payload = task.json()['payload']
    graph = json.loads(Path(payload['workflow']).read_text(encoding='utf-8'))
    staged = payload['execution_snapshot']['staged_materials']
    for node, expected in [('114', image), ('aigc:load-audio-guide', voice)]:
        material = next(item for item in staged if item['node_id'] == node)
        assert (backend.INPUT_ROOT / material['reference']).read_bytes() == expected.read_bytes()
    assert graph['105:104']['inputs']['prompt'] == '水豚抬手问好'
    state = client.get('/api/projects/preset-test/state').json()
    assert not state['reviews']  # Selecting a candidate never approves it.
