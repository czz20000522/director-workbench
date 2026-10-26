"""Anchor dubbing: frozen inputs, explicit cue timing, candidate registration and recovery."""
import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import dubbing

ENDPOINT = '/api/projects/preset-test/dubbing-tasks'
VIDEO_FACTS = {'known': True, 'width': 576, 'height': 1024, 'duration_seconds': 4.0,
               'fps': 24, 'audio_streams': 1}


@pytest.fixture(autouse=True)
def available(monkeypatch):
    monkeypatch.setattr(dubbing, 'capability', lambda engine=None: {'available': True, 'missing': []})
    monkeypatch.setattr(dubbing.creative_inspection, 'probe', lambda path: dict(VIDEO_FACTS))


def inputs(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'registered video')
    reference = tmp_path / 'voice.wav'
    reference.write_bytes(b'voice reference')
    return source, reference


def registered_source(project):
    path = project / 'source.mp4'
    path.write_bytes(b'registered video')
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': 'source', 'kind': 'video', 'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)
    return path


def submit(client, project, **extra):
    reference = project / 'voice.wav'
    reference.write_bytes(b'voice reference')
    body = {'source_asset_id': 'source', 'voice_reference': str(reference),
            'cues': [{'at_seconds': 1, 'text': '第一句'}], 'idempotency_key': 'dub-one', **extra}
    response = client.post(ENDPOINT, json=body)
    assert response.status_code == 202, response.text
    return response.json()


def receipt_for(payload):
    output = Path(payload['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    video, audio = output / 'dubbed.mp4', output / 'dubbed-audio.wav'
    video.write_bytes(b'candidate video')
    audio.write_bytes(b'candidate audio')
    return {'task_id': payload['request']['task_id'], 'status': 'succeeded',
            'output': str(video), 'audio_output': str(audio),
            'duration_seconds': 4.0, 'audio_duration_seconds': 4.0,
            'timeline': [{'index': 1, 'at_seconds': 1.0, 'text': '第一句', 'duration_seconds': 1.5,
                          'end_seconds': 2.5, 'truncated': False, 'gain': 1.0}]}


def test_prepare_copies_inputs_and_freezes_normalized_cues(tmp_path):
    source, reference = inputs(tmp_path)
    payload = dubbing.prepare('task-1', source, reference,
                              [{'at_seconds': 1, 'text': '你好', 'gain': 1.5}], tmp_path / 'tasks',
                              original_audio='keep', original_gain=0.5)
    request, output = dubbing.frozen_request(payload)
    assert request['source_has_audio'] is True
    assert request['cues'] == [{'at_seconds': 1.0, 'text': '你好', 'gain': 1.5}]
    assert Path(request['source']).read_bytes() == b'registered video'
    assert Path(request['voice_reference']).read_bytes() == b'voice reference'
    assert output == (tmp_path / 'tasks' / 'task-1' / 'result').resolve()
    assert json.loads((tmp_path / 'tasks' / 'task-1' / 'request.json').read_text(encoding='utf-8')) == request


@pytest.mark.parametrize('cue', [
    {'at_seconds': -1, 'text': 'x', 'gain': 1},
    {'at_seconds': 4, 'text': 'x', 'gain': 1},
    {'at_seconds': 0, 'text': '   ', 'gain': 1},
    {'at_seconds': 0, 'text': 'x' * 501, 'gain': 1},
    {'at_seconds': 0, 'text': 'x', 'gain': -0.1},
    {'at_seconds': 0, 'text': 'x', 'gain': 4.1},
    {'at_seconds': True, 'text': 'x', 'gain': 1},
    {'at_seconds': 0, 'text': 'x'},
    {'at_seconds': 0, 'text': 'x', 'gain': 1, 'voice': 'y'},
])
def test_prepare_rejects_invalid_cues(tmp_path, cue):
    source, reference = inputs(tmp_path)
    with pytest.raises(ValueError):
        dubbing.prepare('task-1', source, reference, [cue], tmp_path / 'tasks',
                        original_audio='keep', original_gain=1)


def test_prepare_rejects_audio_only_source_and_empty_cues(tmp_path, monkeypatch):
    source, reference = inputs(tmp_path)
    monkeypatch.setattr(dubbing.creative_inspection, 'probe',
                        lambda path: {**VIDEO_FACTS, 'width': None, 'height': None})
    with pytest.raises(ValueError, match='画面轨'):
        dubbing.prepare('task-1', source, reference, [{'at_seconds': 0, 'text': 'x', 'gain': 1}],
                        tmp_path / 'tasks', original_audio='keep', original_gain=1)
    monkeypatch.setattr(dubbing.creative_inspection, 'probe', lambda path: dict(VIDEO_FACTS))
    with pytest.raises(ValueError, match='1 至'):
        dubbing.prepare('task-2', source, reference, [], tmp_path / 'tasks',
                        original_audio='keep', original_gain=1)


def test_frozen_request_rejects_tampered_snapshot_and_foreign_inputs(tmp_path):
    source, reference = inputs(tmp_path)
    payload = dubbing.prepare('task-1', source, reference,
                              [{'at_seconds': 1, 'text': '你好', 'gain': 1}], tmp_path / 'tasks',
                              original_audio='keep', original_gain=1)
    request_path = Path(payload['request_path'])
    changed = json.loads(request_path.read_text(encoding='utf-8'))
    changed['cues'][0]['gain'] = 2
    request_path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='快照'):
        dubbing.frozen_request(payload)

    payload = dubbing.prepare('task-2', source, reference,
                              [{'at_seconds': 1, 'text': '你好', 'gain': 1}], tmp_path / 'tasks',
                              original_audio='keep', original_gain=1)
    with pytest.raises(ValueError, match='输出目录'):
        dubbing.frozen_request({**payload, 'output_dir': str(tmp_path / 'other')})

    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside')
    request_path = Path(payload['request_path'])
    tampered = {**payload['request'], 'source': str(outside)}
    request_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='输入'):
        dubbing.frozen_request({**payload, 'request': tampered})
    assert outside.is_file()


def test_rejects_capability_and_path_like_asset_ids(preset_client, monkeypatch):
    client, project = preset_client
    registered_source(project)
    reference = project / 'voice.wav'
    reference.write_bytes(b'voice reference')
    body = {'source_asset_id': 'source', 'voice_reference': str(reference),
            'cues': [{'at_seconds': 1, 'text': '第一句'}], 'idempotency_key': 'dub-one'}
    monkeypatch.setattr(dubbing, 'capability', lambda engine=None: {'available': False, 'missing': ['IndexTTS']})
    response = client.post(ENDPOINT, json=body)
    assert response.status_code == 503
    assert response.json()['detail']['missing'] == ['IndexTTS']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []
    monkeypatch.setattr(dubbing, 'capability', lambda engine=None: {'available': True, 'missing': []})
    for asset_id in (str(project / 'source.mp4'), 'missing-asset'):
        assert client.post(ENDPOINT, json={**body, 'source_asset_id': asset_id}).status_code == 422
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_submission_freezes_request_and_rejects_changed_parameters(preset_client):
    client, project = preset_client
    registered_source(project)
    task = submit(client, project)
    assert task['status'] == 'scheduler_waiting'
    assert task['payload']['kind'] == 'anchor_dubbing'
    assert task['payload']['request']['cues'] == [{'at_seconds': 1.0, 'text': '第一句', 'gain': 1.0}]
    request_path = Path(task['payload']['request_path'])
    assert request_path.is_file()
    assert client.post(ENDPOINT, json={'source_asset_id': 'source',
                                       'voice_reference': str(project / 'voice.wav'),
                                       'cues': [{'at_seconds': 1, 'text': '第一句'}],
                                       'idempotency_key': 'dub-one'}).json()['id'] == task['id']
    for extra in ({'cues': [{'at_seconds': 1, 'text': '改过的台词'}]}, {'original_audio': 'mute'},
                  {'original_gain': 2}):
        assert client.post(ENDPOINT, json={'source_asset_id': 'source',
                                           'voice_reference': str(project / 'voice.wav'),
                                           'cues': [{'at_seconds': 1, 'text': '第一句'}],
                                           'idempotency_key': 'dub-one', **extra}).status_code == 409
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 1
    assert request_path.is_file()


def test_execution_registers_video_and_audio_candidates(preset_client, monkeypatch):
    client, project = preset_client
    registered_source(project)
    task = submit(client, project)
    monkeypatch.setattr(dubbing, 'execute', lambda payload, cancelled: receipt_for(payload))
    backend.run_anchor_dubbing_task(task['id'], task['payload'])
    completed = backend.get_task(task['id'])
    assert completed['status'] == 'succeeded'
    assert completed['payload']['execution_finished_at']
    assets = backend.require_project_manifest('preset-test')['assets']
    video = next(asset for asset in assets if asset.get('id') == f"dubbing-{task['id']}")
    audio = next(asset for asset in assets if asset.get('id') == f"dubbing-audio-{task['id']}")
    assert video['kind'] == 'video' and video['subtitle'] == '配音候选 · 待检查'
    assert audio['kind'] == 'audio' and audio['subtitle'] == '配音完整音轨 · 待试听'
    assert video['origin'] == 'VoxCPM2 + 原片音轨'
    assert video['source_asset']['asset_id'] == 'source'
    assert video['sources']['A'].endswith('dubbed.mp4')
    assert audio['sources']['A'].endswith('dubbed-audio.wav')
    backend.complete_anchor_dubbing_task(completed, receipt_for(task['payload']))
    assert len(backend.require_project_manifest('preset-test')['assets']) == len(assets)


def test_stop_during_execution_leaves_no_candidates(preset_client, monkeypatch):
    client, project = preset_client
    registered_source(project)
    task = submit(client, project)
    monkeypatch.setattr(dubbing, 'execute', lambda payload, cancelled: receipt_for(payload))
    backend.set_task(task['id'], stop_requested=1)
    backend.run_anchor_dubbing_task(task['id'], task['payload'])
    stopped = backend.get_task(task['id'])
    assert stopped['status'] == 'stopped'
    assert stopped['error'] == '已停止；完成后不登记配音候选'
    assert not [asset for asset in backend.require_project_manifest('preset-test')['assets']
                if str(asset.get('id', '')).startswith('dubbing')]


def test_freeze_failure_marks_task_failed_without_receipt(preset_client, monkeypatch):
    client, project = preset_client
    registered_source(project)
    task = submit(client, project)
    def fail(payload, cancelled):
        raise dubbing.DubbingFailed('adapter exited 1')
    monkeypatch.setattr(dubbing, 'execute', fail)
    backend.run_anchor_dubbing_task(task['id'], task['payload'])
    failed = backend.get_task(task['id'])
    assert failed['status'] == 'failed'
    assert 'adapter exited 1' in failed['error']
    assert not [asset for asset in backend.require_project_manifest('preset-test')['assets']
                if str(asset.get('id', '')).startswith('dubbing')]


def test_reconcile_uses_verified_receipt_instead_of_rerunning(preset_client, monkeypatch):
    client, project = preset_client
    registered_source(project)
    task = submit(client, project)
    receipt = receipt_for(task['payload'])
    backend.set_task(task['id'], status='needs_reconcile')
    monkeypatch.setattr(dubbing, 'read_receipt', lambda payload: receipt)
    response = client.post(f"/api/projects/preset-test/tasks/{task['id']}/reconcile")
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'succeeded'
    assets = backend.require_project_manifest('preset-test')['assets']
    assert {asset['id'] for asset in assets if str(asset.get('id')).startswith('dubbing')} == {
        f"dubbing-{task['id']}", f"dubbing-audio-{task['id']}"}
