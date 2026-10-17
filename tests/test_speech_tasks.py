import json

from backend import app as backend
from test_production_presets import preset_client


def test_flash_parameters_and_legacy_submission_identity(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True})
    reference = project / 'voice.wav'
    reference.write_bytes(b'fixture')
    endpoint = '/api/projects/preset-test/speech-tasks'
    args = {'text': '你好', 'voice_reference': str(reference), 'idempotency_key': 'engine-switch'}
    for extra in ({'gen_seconds': 0}, {'gen_seconds': 31}, {'seed': True}, {'seed': -1}):
        assert client.post(endpoint, json={**args, **extra}).status_code == 422
    first = client.post(endpoint, json=args).json()
    assert first['payload']['engine'] == 'auk-flash'
    assert first['payload']['request']['parameters']['gen_seconds'] == 4.5
    assert client.post(endpoint, json={**args, 'gen_seconds': 6}).status_code == 409
    assert client.post(endpoint, json={**args, 'seed': 42}).status_code == 409
    # Reconstruct the persisted pre-switch receipt; readiness must not affect lookup.
    payload = first['payload']
    payload.pop('engine')
    payload['request'].pop('engine')
    payload['request'].pop('parameters')
    payload['submission_receipt']['request'] = {'kind': 'speech', 'text': args['text'],
                                                'voice_reference': args['voice_reference']}
    backend.set_task(first['id'], payload=json.dumps(payload), status='succeeded')
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': False, 'missing': ['offline']})
    repeated = client.post(endpoint, json=args)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()['id'] == first['id']
    assert 'engine' not in repeated.json()['payload']
    assert client.post(endpoint, json={**args, 'gen_seconds': 4.5}).status_code == 409
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1


def test_speech_submission_receipt_stop_and_recovery(preset_client, monkeypatch, tmp_path):
    client, project = preset_client
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True, 'missing': []})
    reference = project / 'voice.wav'
    reference.write_bytes(b'test reference')
    endpoint = '/api/projects/preset-test/speech-tasks'
    args = {'text': '你好', 'voice_reference': str(reference), 'idempotency_key': 'speech-proof'}
    result = client.post(endpoint, json=args)
    assert result.status_code == 202, result.text
    task = result.json()
    assert task['payload']['kind'] == 'speech'
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'text': '其他台词'}).status_code == 409
    another = client.post(endpoint, json={**args, 'idempotency_key': 'another'})
    assert another.status_code == 202
    assert another.json()['status'] == 'scheduler_waiting'
    assert client.post(f"/api/projects/preset-test/tasks/{another.json()['id']}/stop").json()['status'] == 'stopped'
    detail = f"/api/projects/preset-test/tasks/{task['id']}"
    backend.set_task(task['id'], status='running')
    assert client.post(detail + '/stop').json()['status'] == 'stop_requested'
    def stopped(payload, cancelled):
        assert cancelled()
        raise backend.speech.SpeechStopped('stopped')
    monkeypatch.setattr(backend.speech, 'execute', stopped)
    backend.run_speech_task(task['id'], task['payload'])
    assert client.get(detail).json()['status'] == 'stopped'
    # Recovery never routes a speech task through the video renderer.
    assert client.post(detail + '/resume').status_code == 409
    backend.set_task(task['id'], status='submitting')
    backend.mark_orphans()
    assert client.get(detail).json()['status'] == 'needs_reconcile'
    assert client.post(detail + '/reconcile').status_code == 409
    receipt = {'status': 'succeeded', 'output': str(project / 'speech.wav'), 'duration_seconds': 3.5}
    (project / 'speech.wav').write_bytes(b'validated audio fixture')
    monkeypatch.setattr(backend.speech, 'read_receipt', lambda payload: receipt)
    recovered = client.post(detail + '/reconcile')
    assert recovered.status_code == 200
    assert recovered.json()['status'] == 'succeeded'
    assert recovered.json()['result'] == receipt
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 2
    assets = client.get('/api/projects/preset-test').json()['assets']
    generated = [asset for asset in assets if asset.get('speech_task_id') == task['id']]
    assert len(generated) == 1
    assert generated[0]['kind'] == 'audio'
    assert generated[0]['subtitle'] == '声音候选 · 待试听'
    backend.complete_speech_task(recovered.json(), receipt)
    assert client.get('/api/projects/preset-test').json()['assets'] == assets


def test_missing_speech_environment_rejects_before_creating_task(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': False, 'missing': ['gpt.pth']})
    assert client.get('/api/speech-capability').json()['missing'] == ['gpt.pth']
    response = client.post('/api/projects/preset-test/speech-tasks', json={'text': '你好', 'voice_reference': 'missing.wav', 'idempotency_key': 'missing-runtime'})
    assert response.status_code == 503
    assert response.json()['detail']['missing'] == ['gpt.pth']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_definite_failure_releases_capacity_without_automatic_retry(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True, 'missing': []})
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    reference = project / 'voice.wav'
    reference.write_bytes(b'fixture')
    args = {'text': '你好', 'voice_reference': str(reference), 'idempotency_key': 'failed-proof'}
    endpoint = '/api/projects/preset-test/speech-tasks'
    task = client.post(endpoint, json=args).json()
    def fail(*args):
        raise backend.speech.SpeechFailed('process exited')
    monkeypatch.setattr(backend.speech, 'execute', fail)
    backend.run_speech_task(task['id'], task['payload'])
    assert backend.get_task(task['id'])['status'] == 'failed'
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    new = client.post(endpoint, json={**args, 'idempotency_key': 'new-operation'})
    assert new.status_code == 202
    assert new.json()['id'] != task['id']


def test_design_freezes_description_and_rejects_ambiguous_inputs(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(backend.speech, 'capability', lambda: {'available': True})
    endpoint = '/api/projects/preset-test/speech-tasks'
    args = {'text': '福贵啊，你坐下。', 'mode': 'design', 'voice_description': '低沉苍老的男声', 'idempotency_key': 'design-voice'}
    for extra in ({'voice_reference': 'other.wav'}, {'reference_image': 'other.png'}, {'voice_description': ' '}, {'mode': 'reference'}, {'mode': 'invalid'}):
        assert client.post(endpoint, json={**args, **extra}).status_code == 422
    result = client.post(endpoint, json=args)
    assert result.status_code == 202, result.text
    task = result.json()
    frozen = task['payload']['request']
    assert frozen['mode'] == 'design'
    assert frozen['voice_description'] == args['voice_description']
    assert 'voice_reference' not in frozen
    assert not list((project / 'speech-tasks' / task['id']).glob('voice-reference*'))
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'voice_description': '青年男声'}).status_code == 409
    assert client.post(endpoint, json={**args, 'seed': 3}).status_code == 409


def test_design_receipt_rejects_changed_description(tmp_path):
    import wave
    import pytest
    from backend import speech
    payload = speech.prepare('design-receipt', '你好。', None, tmp_path, engine='auk-flash',
                             mode='design', voice_description='青年男声')
    from pathlib import Path
    output_dir = Path(payload['output_dir'])
    output_dir.mkdir()
    output = output_dir / 'speech.wav'
    with wave.open(str(output), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b'\0\0' * 2400)
    receipt = {**payload['request'], 'status': 'succeeded', 'output': str(output),
               'sample_rate': 24000, 'channels': 1, 'duration_seconds': .1}
    receipt_path = output_dir / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    assert speech.read_receipt(payload)['voice_description'] == '青年男声'
    receipt['voice_description'] = '另一个声音'
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(ValueError, match='冻结输入'):
        speech.read_receipt(payload)
