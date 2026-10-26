"""换音轨：成片后只改音频，画面流不重渲染。"""
import json
import subprocess
from pathlib import Path

import pytest

from backend import app as backend
from backend import audio_swap, media_operations

VIDEO_FACTS = {'known': True, 'width': 576, 'height': 1024, 'duration_seconds': 4.0,
               'fps': 24, 'audio_streams': 1}
PARAMS = {'original_audio': 'keep', 'original_gain': 1, 'overlay_gain': 1, 'start_seconds': 0,
          'overlay_loop': True, 'fade_in_seconds': 0, 'fade_out_seconds': 0}


@pytest.fixture
def fake_probe(monkeypatch):
    monkeypatch.setattr(audio_swap.creative_inspection, 'probe', lambda path: dict(VIDEO_FACTS))


def inputs(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'video bytes')
    overlay = tmp_path / 'music.wav'
    overlay.write_bytes(b'music bytes')
    return source, overlay


def test_prepare_freezes_copies_and_request_shape(tmp_path, fake_probe):
    source, overlay = inputs(tmp_path)
    payload = audio_swap.prepare('swap-1', source, overlay, tmp_path / 'tasks', PARAMS)
    request, output = audio_swap.frozen_request(payload)
    assert request['original_audio'] == 'keep' and request['overlay_loop'] is True
    assert Path(request['source']).read_bytes() == b'video bytes'
    assert Path(request['overlay']).read_bytes() == b'music bytes'
    assert output == (tmp_path / 'tasks' / 'swap-1' / 'result').resolve()
    source.write_bytes(b'changed after freeze')
    assert Path(request['source']).read_bytes() == b'video bytes'


def test_prepare_without_overlay_is_pure_silence_or_original(tmp_path, fake_probe):
    source, _ = inputs(tmp_path)
    payload = audio_swap.prepare('swap-2', source, None, tmp_path / 'tasks',
                                 {**PARAMS, 'original_audio': 'mute', 'overlay_gain': 0,
                                  'overlay_loop': False})
    assert 'overlay' not in payload['request']
    request, _ = audio_swap.frozen_request(payload)
    assert request['source_has_audio'] is True and request['original_audio'] == 'mute'


def test_parameters_are_validated_not_guessed():
    with pytest.raises(ValueError, match='原声'):
        audio_swap.validate({**PARAMS, 'original_audio': 'drop'}, VIDEO_FACTS, True)
    with pytest.raises(ValueError, match='音量'):
        audio_swap.validate({**PARAMS, 'overlay_gain': 4}, VIDEO_FACTS, True)
    with pytest.raises(ValueError, match='开始时间'):
        audio_swap.validate({**PARAMS, 'start_seconds': 9}, VIDEO_FACTS, True)
    with pytest.raises(ValueError, match='循环'):
        audio_swap.validate({**PARAMS, 'overlay_loop': 'yes'}, VIDEO_FACTS, True)
    with pytest.raises(ValueError, match='没有音轨'):
        audio_swap.validate({**PARAMS, 'original_audio': 'keep'}, {**VIDEO_FACTS, 'audio_streams': 0}, True)
    inert = audio_swap.validate({**PARAMS, 'overlay_gain': 0.5, 'start_seconds': 2}, VIDEO_FACTS, False)
    assert (inert['overlay_gain'], inert['start_seconds'], inert['overlay_loop']) == (0.0, 0.0, False)
    with pytest.raises(ValueError, match='画面'):
        audio_swap.validate(PARAMS, {**VIDEO_FACTS, 'width': None}, True)


def test_command_keeps_mute_and_overlay_branches_honest():
    base = {'source': 'S.mp4', 'source_duration_seconds': 4.0, 'original_gain': 0.5,
            'overlay_gain': 1.2, 'start_seconds': 1.5, 'overlay_loop': True,
            'fade_in_seconds': 0.5, 'fade_out_seconds': 1.0}
    muted = audio_swap.build_command({**base, 'original_audio': 'mute', 'overlay': 'M.wav'}, Path('out.mp4'))
    graph = muted[muted.index('-filter_complex') + 1]
    assert 'anullsrc' in graph and 'adelay=1500|1500' in graph and 'afade=t=in' in graph
    assert 'afade=t=out:st=3.000:d=1.0' in graph and 'amix=inputs=2' in graph
    assert muted[muted.index('-c:v') + 1] == 'copy'
    assert '-stream_loop' in muted
    kept = audio_swap.build_command({**base, 'original_audio': 'keep', 'overlay': 'M.wav'}, Path('out.mp4'))
    assert '[0:a:0]aresample=48000' in kept[kept.index('-filter_complex') + 1]
    silent = audio_swap.build_command({**base, 'original_audio': 'keep'}, Path('out.mp4'))
    silent_graph = silent[silent.index('-filter_complex') + 1]
    assert 'amix=inputs=1' in silent_graph and '[1:a:0]' not in silent_graph


real_media = pytest.mark.skipif(not media_operations.FFMPEG.is_file(), reason='需要 FFmpeg')


@real_media
def test_real_ffmpeg_swaps_only_the_audio(tmp_path, monkeypatch):
    ffmpeg = str(media_operations.FFMPEG)
    source = tmp_path / 'source.mp4'
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=24:duration=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-shortest',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True, timeout=120)
    music = tmp_path / 'music.wav'
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1',
                    '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(music)], check=True, timeout=120)
    payload = audio_swap.prepare('swap-real', source, music, tmp_path / 'tasks',
                                 {**PARAMS, 'original_gain': 0.2, 'overlay_gain': 0.8,
                                  'fade_out_seconds': 0.4})
    receipt = audio_swap.execute(payload)
    assert abs(receipt['duration_seconds'] - 2.0) <= 0.25
    assert abs(receipt['audio_duration_seconds'] - 2.0) <= 0.25
    assert Path(receipt['output']).is_file() and Path(receipt['audio_output']).is_file()
    assert audio_swap.read_receipt(payload) == receipt


def submit(client, project, **extra):
    video = project / 'shot.mp4'
    video.write_bytes(b'video bytes')
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': 'shot', 'kind': 'video', 'title': '镜头 1',
                               'sources': {'A': str(video)}})
    manifest['assets'].append({'id': 'score', 'kind': 'audio', 'title': '配乐候选',
                               'sources': {'A': str(project / 'score.wav')}})
    (project / 'score.wav').write_bytes(b'music bytes')
    backend.save_project_manifest(manifest)
    body = {'source_asset_id': 'shot', 'overlay_asset_id': 'score',
            'idempotency_key': 'swap-one', **extra}
    response = client.post('/api/projects/preset-test/audio-swap-tasks', json=body)
    return response, body


def test_submission_freezes_and_requires_current_assets(preset_client, monkeypatch, fake_probe):
    client, project = preset_client
    monkeypatch.setattr(audio_swap, 'execute', lambda payload, cancelled: {
        'task_id': payload['request']['task_id'], 'status': 'succeeded',
        'output': str(Path(payload['output_dir']) / 'x.mp4'),
        'audio_output': str(Path(payload['output_dir']) / 'x.wav'),
        'duration_seconds': 4.0, 'audio_duration_seconds': 4.0})
    response, body = submit(client, project)
    assert response.status_code == 202, response.text
    task = response.json()
    assert task['payload']['kind'] == 'audio_swap'
    assert task['payload']['request']['overlay'].endswith('overlay.wav')
    assert client.post('/api/projects/preset-test/audio-swap-tasks', json=body).json()['id'] == task['id']
    changed = client.post('/api/projects/preset-test/audio-swap-tasks', json={**body, 'original_audio': 'mute'})
    assert changed.status_code == 409
    for bad in ({'source_asset_id': 'score'}, {'overlay_asset_id': 'shot'}, {'overlay_asset_id': 'missing'}):
        response = client.post('/api/projects/preset-test/audio-swap-tasks', json={**body, **bad, 'idempotency_key': 'other'})
        assert response.status_code == 422, response.text


def test_execution_registers_video_and_audio_candidates(preset_client, monkeypatch, fake_probe):
    client, project = preset_client
    response, _ = submit(client, project)
    task = response.json()

    def execute(payload, cancelled):
        output = Path(payload['output_dir'])
        output.mkdir(parents=True, exist_ok=True)
        video, audio = output / 'swapped.mp4', output / 'swapped-audio.wav'
        video.write_bytes(b'candidate video')
        audio.write_bytes(b'candidate audio')
        return {'task_id': payload['request']['task_id'], 'status': 'succeeded',
                'output': str(video), 'audio_output': str(audio),
                'duration_seconds': 4.0, 'audio_duration_seconds': 4.0}
    monkeypatch.setattr(audio_swap, 'execute', execute)
    backend.run_audio_swap_task(task['id'], task['payload'])
    completed = backend.get_task(task['id'])
    assert completed['status'] == 'succeeded', completed.get('error')
    assets = backend.require_project_manifest('preset-test')['assets']
    video = next(asset for asset in assets if asset.get('id') == f"audio-swap-{task['id']}")
    audio = next(asset for asset in assets if asset.get('id') == f"audio-swap-audio-{task['id']}")
    assert video['kind'] == 'video' and video['subtitle'] == '换音轨候选 · 待检查'
    assert audio['kind'] == 'audio' and audio['subtitle'] == '音轨候选 · 待试听'
    assert video['origin'] == 'FFmpeg 换音轨' and video['sources']['A'].endswith('swapped.mp4')
    assert video['source_asset']['asset_id'] == 'shot'
    assert audio['sources']['A'].endswith('swapped-audio.wav')
