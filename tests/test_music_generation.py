"""ACE-Step 配乐：显式参数校验、冻结工作流与音频候选登记。"""
import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import music

ENDPOINT = '/api/projects/preset-test/music-tasks'
PARAMS = {'tags': '悬疑弦乐，低频铺底', 'lyrics': '', 'duration_seconds': 15, 'bpm': 90, 'keyscale': 'A minor',
          'timesignature': '4', 'language': 'unknown', 'seed': 7, 'steps': 8, 'cfg': 1.0, 'shift': 3.0,
          'output_format': 'mp3', 'quality': '128k', 'generate_audio_codes': True, 'encode_cfg_scale': 2.0,
          'temperature': 0.85, 'top_p': 0.9, 'top_k': 0, 'min_p': 0.0, 'idempotency_key': 'music-one'}


@pytest.fixture(autouse=True)
def engine_ready(monkeypatch, tmp_path):
    checkpoint = tmp_path / 'checkpoints' / music.CHECKPOINT
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b'fixture checkpoint')
    monkeypatch.setattr(music, 'CHECKPOINT_PATH', checkpoint)
    info = {name: {'input': {}} for name in music.NODES}
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: info)


def submit(client, **extra):
    response = client.post(ENDPOINT, json={**PARAMS, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def frozen_params(**extra):
    body = {**PARAMS, **extra}
    body.pop('idempotency_key')
    return music.validate({**body, 'format': body['output_format']})


def test_validate_accepts_explicit_instrumental_request():
    frozen = frozen_params()
    assert frozen['lyrics'] == '' and frozen['keyscale'] == 'A minor'
    assert frozen['duration_seconds'] == 15.0 and frozen['bpm'] == 90


@pytest.mark.parametrize('extra', [
    {'tags': '   '}, {'tags': '弦' * 2001}, {'lyrics': '词' * 4001}, {'duration_seconds': 0.5},
    {'duration_seconds': 301}, {'bpm': 301}, {'keyscale': 'H minor'}, {'timesignature': '5'},
    {'language': 'klingon'}, {'steps': 0}, {'steps': 61}, {'seed': -1}, {'cfg': 11}, {'shift': -1},
    {'output_format': 'wav'}, {'quality': '999k'}, {'top_k': 101}, {'min_p': 1.5}, {'temperature': True},
])
def test_validate_rejects_out_of_range_parameters(extra):
    with pytest.raises(ValueError):
        frozen_params(**extra)


def test_build_graph_mirrors_frozen_parameters_and_prefix():
    frozen = frozen_params()
    graph = music.build_graph(frozen, prefix='导演工作台工作目录/p/music/t')
    assert sorted(graph) == ['decode', 'encode', 'guidance', 'latent', 'loader', 'negative', 'sampler', 'save']
    assert graph['loader']['inputs'] == {'ckpt_name': music.CHECKPOINT}
    assert graph['encode']['inputs']['tags'] == frozen['tags']
    assert graph['encode']['inputs']['duration'] == 15.0 and graph['encode']['inputs']['bpm'] == 90
    assert graph['latent']['inputs']['seconds'] == 15.0
    assert graph['sampler']['inputs']['seed'] == 7 and graph['sampler']['inputs']['steps'] == 8
    assert graph['save']['class_type'] == 'SaveAudioMP3'
    assert graph['save']['inputs'] == {'audio': ['decode', 0], 'filename_prefix': '导演工作台工作目录/p/music/t',
                                       'quality': '128k'}
    assert music.build_graph(frozen_params(output_format='flac'), prefix='x')['save']['class_type'] == 'SaveAudio'


def test_capability_reports_missing_nodes_and_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(music, 'CHECKPOINT_PATH', tmp_path / 'absent.safetensors')
    report = music.capability({name: {} for name in music.NODES})
    assert report['available'] is False and report['missing_models'] == [f'checkpoints/{music.CHECKPOINT}']
    report = music.capability({'CheckpointLoaderSimple': {}})
    assert 'TextEncodeAceStepAudio1.5' in report['missing_nodes']


def test_candidate_output_requires_one_audio_file_inside_output_root(preset_client, monkeypatch):
    client, project = preset_client
    output = backend.OUTPUT_ROOT / '导演工作台工作目录' / 'preset-test' / 'music' / 't'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'song_00001_.mp3').write_bytes(b'audio candidate')
    relative = '导演工作台工作目录/preset-test/music/t/song_00001_.mp3'
    assert music.candidate_output({'outputs': [relative]}) == (output / 'song_00001_.mp3').resolve()
    for outputs in ([], [relative, relative], ['../../outside.mp3']):
        with pytest.raises(ValueError):
            music.candidate_output({'outputs': outputs})


def test_completion_registers_audio_candidate_without_adopting(preset_client):
    client, project = preset_client
    task = submit(client)
    output = backend.OUTPUT_ROOT / '导演工作台工作目录' / 'preset-test' / 'music' / task['id']
    output.mkdir(parents=True, exist_ok=True)
    media = output / 'music_00001_.mp3'
    media.write_bytes(b'audio candidate')
    current = backend.get_task(task['id'])
    current['payload']['music'] = task['payload']['music']
    done = {'status': 'success', 'outputs': [f'导演工作台工作目录/preset-test/music/{task["id"]}/music_00001_.mp3'],
            'raw_status': {}, 'comfy_timing': {}}
    completed = music.complete_music_task(current, done)
    assert completed['status'] == 'succeeded'
    assert Path(completed['result']['output']).is_file()
    assets = backend.require_project_manifest('preset-test')['assets']
    asset = next(item for item in assets if item['id'] == f"music-{task['id']}")
    assert asset['kind'] == 'audio' and asset['subtitle'] == '配乐候选 · 待审核'
    assert asset['origin'] == 'ACE-Step 1.5 Turbo' and asset['end'] == 15.0
    assert 'adopted' not in asset and not asset.get('selected')
    music.complete_music_task(backend.get_task(task['id']), done)
    assert len([item for item in backend.require_project_manifest('preset-test')['assets']
                if item['id'] == f"music-{task['id']}"]) == 1


def test_completion_after_stop_keeps_task_stopped_and_unregistered(preset_client):
    client, project = preset_client
    task = submit(client)
    backend.set_task(task['id'], stop_requested=1)
    current = backend.get_task(task['id'])
    stopped = music.complete_music_task(current, {'status': 'success', 'outputs': []})
    assert stopped['status'] == 'stopped'
    assert not [item for item in backend.require_project_manifest('preset-test')['assets']
                if str(item.get('id')).startswith('music')]


def test_submission_freezes_graph_and_rejects_changed_parameters(preset_client):
    client, project = preset_client
    task = submit(client)
    assert task['status'] == 'scheduler_waiting'
    assert task['payload']['kind'] == 'music'
    workflow = Path(task['payload']['workflow'])
    assert workflow.is_file()
    graph = json.loads(workflow.read_text(encoding='utf-8'))
    assert graph['encode']['inputs']['keyscale'] == 'A minor'
    assert task['payload']['music']['duration_seconds'] == 15.0
    assert client.post(ENDPOINT, json=PARAMS).json()['id'] == task['id']
    assert client.post(ENDPOINT, json={**PARAMS, 'bpm': 96}).status_code == 409
    assert client.post(ENDPOINT, json={**PARAMS, 'idempotency_key': 'music-invalid', 'keyscale': 'H minor'}).status_code == 422
    assert client.post(ENDPOINT, json={**PARAMS, 'idempotency_key': 'music-two'}).status_code == 202
    assert len(client.get('/api/projects/preset-test/tasks').json()['tasks']) == 2


def test_missing_runtime_rejects_before_creating_task(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(music, 'CHECKPOINT_PATH', Path('D:/absent/ace.safetensors'))
    report = client.get('/api/music-capability').json()
    assert report['available'] is False and report['missing_models']
    response = client.post(ENDPOINT, json=PARAMS)
    assert response.status_code == 503
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_history_done_collects_audio_artifacts_only_for_audio(preset_client, monkeypatch):
    client, _ = preset_client
    history = {'pid': {'status': {'status_str': 'success'}, 'outputs': {'save': {
        'audio': [{'filename': 'track_00001.mp3', 'subfolder': '导演工作台工作目录\\p\\music', 'type': 'output'}]}}}}
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: history)
    assert backend.history_done('pid')['outputs'] == []
    audio = backend.history_done('pid', audio=True)
    assert audio['outputs'] == ['导演工作台工作目录\\p\\music/track_00001.mp3']
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: {
        'pid': {'status': {'status_str': 'success'}, 'outputs': {'save': {
            'images': [{'filename': 'frame.png', 'subfolder': '', 'type': 'output'}]}}}})
    assert backend.history_done('pid', audio=True)['outputs'] == []


def test_reconcile_refuses_a_missing_audio_candidate(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    history = {'status': {'status_str': 'success'}, 'outputs': {'save': {'audio': [
        {'filename': 'gone_00001.mp3', 'subfolder': '导演工作台工作目录\\preset-test\\music', 'type': 'output'}]}}}

    def fake(path, *args, **kwargs):
        if path == '/queue':
            return {'queue_running': [], 'queue_pending': []}
        if path.startswith('/history/'):
            return {task['id']: history}
        raise AssertionError(path)
    monkeypatch.setattr(backend, 'request_json', fake)
    backend.set_task(task['id'], status='needs_reconcile', prompt_id=task['id'])
    response = client.post(f"/api/projects/preset-test/tasks/{task['id']}/reconcile")
    assert response.status_code == 409, response.text
    assert '输出文件缺失' in response.json()['detail']
    assert backend.get_task(task['id'])['status'] == 'needs_reconcile'
    assert not [item for item in backend.require_project_manifest('preset-test')['assets']
                if str(item.get('id')).startswith('music')]


def test_reconcile_recovers_the_stuck_audio_run(preset_client, monkeypatch):
    client, _ = preset_client
    task = submit(client)
    output = backend.OUTPUT_ROOT / '导演工作台工作目录' / 'preset-test' / 'music'
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{task['id']}_recovered_00001.mp3").write_bytes(b'recovered audio candidate')
    history = {'status': {'status_str': 'success'}, 'outputs': {'save': {'audio': [
        {'filename': f"{task['id']}_recovered_00001.mp3", 'subfolder': '导演工作台工作目录\\preset-test\\music',
         'type': 'output'}]}}}

    def fake(path, *args, **kwargs):
        if path == '/queue':
            return {'queue_running': [], 'queue_pending': []}
        if path.startswith('/history/'):
            return {task['id']: history}
        raise AssertionError(path)
    monkeypatch.setattr(backend, 'request_json', fake)
    backend.set_task(task['id'], status='needs_reconcile', prompt_id=task['id'])
    recovered = client.post(f"/api/projects/preset-test/tasks/{task['id']}/reconcile")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'succeeded'
    assert Path(recovered.json()['result']['output']).is_file()
    asset = next(item for item in backend.require_project_manifest('preset-test')['assets']
                 if item['id'] == f"music-{task['id']}")
    assert asset['subtitle'] == '配乐候选 · 待审核'


def test_music_task_dispatches_through_the_workflow_runner(preset_client, monkeypatch):
    client, project = preset_client
    task = submit(client)
    calls = []
    monkeypatch.setattr(backend, 'run_workflow_submission', lambda task_id, payload: calls.append(task_id))
    backend.execute_scheduled_task(backend.get_task(task['id']))
    assert calls == [task['id']]
    assert backend.get_task(task['id'])['status'] == 'scheduler_waiting'
