"""DW017-020: real tiny CPU files, isolated databases and shared HTTP/MCP services."""
import asyncio
import importlib.util
import json
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
from mcp import Client

from backend import app as backend, creative_inspection, h3_duration, media_operations, audio_edit
from mcp_server.server import create_server
from test_production_presets import preset_client as base_preset_client, add_shot
from test_assembly_edit import edit_client
from test_private_workbench import private_workbench, create
from test_segment_versions import generated_version

ROOT = '/api/projects/preset-test'
BUILDER = Path(__file__).resolve().parents[1] / 'tools/build_assembly_workflow.py'
REAL_THREAD = threading.Thread


@pytest.fixture
def preset_client(base_preset_client, monkeypatch):
    # Disable dispatch at the task boundary, preserving real subprocess pipe readers.
    monkeypatch.setattr(backend.threading, 'Thread', REAL_THREAD)
    monkeypatch.setattr(backend, 'queue_task', lambda task_id: None)
    return base_preset_client


def ffmpeg(*args):
    assert media_operations.FFMPEG.is_file(), 'Existing local FFmpeg required; never download'
    subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-n', *map(str, args)],
                   check=True, timeout=30, capture_output=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def video(path, size='640x640', frames=12):
    ffmpeg('-f', 'lavfi', '-i', f'color=c=red:s={size}:r=24', '-frames:v', frames,
           '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', path)
    return path


def wav(path, seconds=1, rate=48000, channels=2):
    samples = (np.sin(np.arange(round(seconds * rate)) * 2 * np.pi * 440 / rate) * 8192).astype('<i2')
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(np.repeat(samples[:, None], channels, axis=1).tobytes())
    return path


def register(project_id, asset_id, path, kind):
    manifest = backend.require_project_manifest(project_id)
    manifest['assets'].append({'id': asset_id, 'kind': kind, 'title': asset_id, 'sources': {'A': str(path)}})
    backend.save_project_manifest(manifest)


def configured(preset_client, preset='h3-first-native-landscape'):
    client, project = preset_client
    response = client.post(ROOT + '/production-presets/' + preset)
    assert response.status_code == 200, response.text
    add_shot(client, project, 'S01', with_audio=False)
    image = project / 'workspaces/preset-test/assets/S01.png'
    image.rename(image.with_name('original-test-image.png'))
    ffmpeg('-f', 'lavfi', '-i', 'color=s=1024x1024', '-frames:v', 1, image)
    manifest = backend.require_project_manifest('preset-test')
    manifest['assembly']['builder'] = str(BUILDER)
    backend.save_project_manifest(manifest)
    return client, project, backend.production_presets.stage_id(preset)


@pytest.mark.parametrize(('preset', 'width', 'different'), [
    ('h3-first-native-landscape', 864, True), ('h3-first-native-square', 640, False),
])
def test_reference_differences_preflight_frozen_receipt_and_no_automatic_changes(preset_client, preset, width, different):
    client, project, stage = configured(preset_client, preset)
    source = video(project / 'reference.mp4')
    register('preset-test', 'reference', source, 'video')
    saved = client.patch(ROOT + '/plan/segments/S01', json={'reference_asset_id': 'reference'})
    assert saved.status_code == 200, saved.text
    before = client.get(ROOT + '/plan').json()
    checked = client.post(ROOT + f'/pipeline/{stage}/validate', json={'asset_id': 'S01', 'values': {}})
    assert checked.status_code == 200, checked.text
    summary = checked.json()['creative_inspection']
    difference = summary['reference_comparison']
    assert difference['reference']['facts']['width'] == 640
    assert difference['planned']['width'] == width
    assert difference['aspect_ratio'] == ('different' if different else 'same')
    assert difference['first_frame']['preprocessing'] == 'stretch'
    assert difference['first_frame']['aspect_mismatch'] is different
    assert difference['blocking'] is False
    assert client.post(ROOT + f'/pipeline/{stage}/validate', json={'asset_id': 'S01', 'values': {}}).json()['creative_inspection'] == summary
    assert client.get(ROOT + '/plan').json() == before
    assert client.get(ROOT + '/tasks').json()['tasks'] == []
    result = client.post(ROOT + '/tasks', json={'asset_id': 'S01', 'pipeline_stage_id': stage, 'pipeline_values': {}, 'idempotency_key': 'isolated-freeze'})
    assert result.status_code == 200, result.text
    task = result.json()  # No-op test thread: does not dispatch ComfyUI.
    assert task['payload']['execution_snapshot']['creative_inspection'] == summary
    assert task['payload']['h3_duration']['width'] == width
    assert client.post(ROOT + '/tasks', json={'asset_id': 'S01', 'pipeline_stage_id': stage, 'pipeline_values': {}, 'idempotency_key': 'isolated-freeze'}).json()['id'] == task['id']
    document, path = backend.load_project_plan('preset-test')
    document['segments'][0]['workflow'] = task['payload']['workflow']
    backend.write_plan_version(path, document)
    # A completed candidate's frozen recipe remains recognisable without rewriting it.
    candidate = client.get(ROOT + '/segments/S01/versions').json()['inspection']
    assert candidate['reference_comparison']['planned']['width'] == width
    assert candidate['timing']['expected_candidate']['frame_count'] == task['payload']['h3_duration']['frame_count']
    (project / 'preflight-browser.json').write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')


@pytest.mark.parametrize('seconds', [4, 4.7, 7.3, 8.5, 15])
def test_duration_contract_saved_preflight_versions_and_real_candidate(preset_client, seconds):
    client, project, stage = configured(preset_client)
    assert client.patch(ROOT + '/plan/segments/S01', json={'duration_seconds': seconds}).status_code == 200
    checked = client.post(ROOT + f'/pipeline/{stage}/validate', json={'asset_id': 'S01', 'values': {}}).json()
    expected = h3_duration.plan(seconds, '16:9')
    timing = checked['creative_inspection']['timing']
    assert timing['requested_seconds'] == seconds
    assert timing['expected_candidate']['frame_count'] == expected['frame_count']
    assert timing['observed_candidate']['known'] is False
    assert timing['assembly_take']['frame_count'] == round(seconds * 24)
    real = video(project / 'observed.mp4', '864x480', frames=124)
    document, path = backend.load_project_plan('preset-test')
    document['segments'][0]['video'] = {'path': str(real)}
    document['segments'][0]['current_version_task_id'] = 'measured-candidate'
    backend.write_plan_version(path, document)
    before = path.read_bytes()
    versions = client.get(ROOT + '/segments/S01/versions').json()
    timing = versions['inspection']['timing']
    assert timing['observed_candidate']['frames'] == 124
    assert timing['observed_candidate']['duration_seconds'] == pytest.approx(124 / 24, abs=1e-6)
    assert timing['expected_candidate']['frame_count'] == expected['frame_count']
    assert client.get(ROOT + '/assembly/edit').json()['timing_summary']['frame_count'] == round(seconds * 24)
    async def verify():
        async with Client(create_server('http://localhost', httpx.ASGITransport(app=backend.app))) as agent:
            for tool, args, expected_response in (
                ('list_shot_versions', {'project_id': 'preset-test', 'segment_id': 'S01'}, versions),
                ('preflight', {'project_id': 'preset-test', 'stage_id': stage, 'asset_id': 'S01', 'values': {}},
                 client.post(ROOT + f'/pipeline/{stage}/validate', json={'asset_id': 'S01', 'values': {}}).json()),
            ):
                data = (await agent.call_tool(tool, args)).structured_content
                assert data['ok'] and data['data'] == expected_response
    asyncio.run(verify())
    assert path.read_bytes() == before
    assert client.get(ROOT + '/tasks').json()['tasks'] == []
    (project / 'creative-browser.json').write_text(json.dumps(versions, ensure_ascii=False), encoding='utf-8')


def test_unknown_corrupt_reference_non_h3_and_probe_cache_follow_file_change(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01', with_audio=False)
    no_reference = client.get(ROOT + '/segments/S01/versions').json()['inspection']
    assert no_reference['reference_comparison']['reference'] is None
    assert no_reference['timing']['expected_candidate'] is None
    source = project / 'corrupt.mp4'
    source.write_bytes(b'not a video')
    register('preset-test', 'reference', source, 'video')
    assert client.patch(ROOT + '/plan/segments/S01', json={'reference_asset_id': 'reference'}).status_code == 200
    before = client.get(ROOT + '/plan').json()
    data = client.get(ROOT + '/segments/S01/versions').json()
    assert data['inspection']['reference_comparison']['reference']['facts']['known'] is False
    assert all(row['status'] == 'unknown' for row in data['inspection']['reference_comparison']['differences'][:-1])
    source.rename(project / 'corrupt-retained.mp4')
    video(source)
    assert creative_inspection.probe(source)['width'] == 640
    assert client.get(ROOT + '/plan').json() == before


def test_version_comparison_impact_tracks_real_existing_invalidation(preset_client):
    client, project, _ = configured(preset_client)
    add_shot(client, project, 'S02', with_audio=False)
    reference = video(project / 'reference.mp4', frames=48)
    register('preset-test', 'reference', reference, 'video')
    assert client.patch(ROOT + '/plan/segments/S01', json={'reference_asset_id': 'reference'}).status_code == 200
    doc, plan_path = backend.load_project_plan('preset-test')
    doc['segments'][0]['workflow'] = 'workflow.json'
    doc['segments'][1]['dependencies'] = ['S01']
    other = video(project / 'right.mp4')
    doc['segments'][1]['video'] = {'path': str(other)}
    doc['segments'][1]['current_version_task_id'] = 'right-fixture'
    doc['assembly'] = {'output': str(other), 'status': 'assembled'}
    backend.write_plan_version(plan_path, doc)
    first = generated_version(client, project, 'first', '旧提示词', 100)
    real = video(project / 'real.mp4')
    shutil.copyfile(real, first)
    revision = client.get(ROOT + '/plan').json()['revision']
    assert client.put(ROOT + '/assembly/joins/S01/S02', json={'expected_revision': revision, 'status': 'approved', 'note': 'isolated explicit review'}).status_code == 200
    before = client.get(ROOT + '/segments/S01/versions').json()
    effects = before['change_impact']['effects']
    assert any(row['kind'] == 'dependency_input' and row['effect'] == 'needs_recheck' for row in effects)
    assert any(row['kind'] == 'join_review' and row['effect'] == 'stale_when_video_changes' for row in effects)
    second = generated_version(client, project, 'second', '只修改提示词', 200)
    shutil.copyfile(real, second)
    after = client.get(ROOT + '/segments/S01/versions').json()
    old = next(row for row in after['items'] if row['task_id'] == 'first')
    assert old['snapshot']['prompt'] == '旧提示词'
    assert old['parameter_differences'] == [{'field': 'prompt', 'current': '只修改提示词', 'previous': '旧提示词'}]
    assert first.is_file() and second.is_file()
    assert client.get(ROOT + '/assembly/edit').json()['joins'][0]['status'] == 'stale'
    assert client.get(ROOT + '/plan').json()['assembly']['status'] == 'stale'
    assert before['revision'] < after['revision']
    (project / 'comparison-browser.json').write_text(json.dumps(after, ensure_ascii=False), encoding='utf-8')


def test_reference_and_comparison_are_private_owner_scoped(private_workbench):
    a, b, _, _ = private_workbench
    first, second = create(a), create(b)
    root = '/api/projects/' + first['id']
    assert a.post(root + '/plan/segments', json={'segment_id': 'S01', 'duration_seconds': 5}).status_code == 200
    assert b.get(root + '/segments/S01/versions').status_code == 404
    before = a.get(root + '/plan').json()
    rejected = a.patch(root + '/plan/segments/S01', json={'reference_asset_id': 'another-project'})
    assert rejected.status_code == 422
    assert a.get(root + '/plan').json() == before
    assert b.patch(root + '/plan/segments/S01', json={'reference_asset_id': 'another-project'}).status_code == 404


def test_mp3_cpu_edit_frozen_receipt_gain_fades_padding_and_idempotency(preset_client, monkeypatch):
    client, project = preset_client
    source = wav(project / 'original.wav')
    mp3 = project / 'original.mp3'
    ffmpeg('-i', source, '-c:a', 'libmp3lame', mp3)
    assert creative_inspection.probe(mp3)['duration_seconds'] > 0
    register('preset-test', 'mp3', mp3, 'audio')
    monkeypatch.setattr(backend, 'request_json', lambda *a, **k: pytest.fail('CPU called ComfyUI'))
    before = client.get(ROOT + '/plan').json()
    body = {'source_asset_id': 'mp3', 'idempotency_key': 'cpu-frozen', 'duration_seconds': 1.5,
            'gain': .5, 'fade_in_seconds': .1, 'fade_out_seconds': .2, 'pad_silence': True}
    response = client.post(ROOT + '/audio-edit-tasks', json=body)
    assert response.status_code == 202, response.text
    task = response.json()
    assert task['payload']['request']['parameters']['pad_silence'] is True
    assert client.post(ROOT + '/audio-edit-tasks', json=body).json()['id'] == task['id']
    assert client.post(ROOT + '/audio-edit-tasks', json={**body, 'pad_silence': False}).status_code == 409
    original = mp3.read_bytes()
    mp3.write_bytes(b'source subsequently changed')
    assert Path(task['payload']['request']['source']).read_bytes() == original
    backend.run_audio_edit_task(task['id'], task['payload'])
    finished = backend.get_task(task['id'])
    assert finished['status'] == 'succeeded', finished
    receipt = finished['result']
    assert receipt['frames'] == 72000 and receipt['duration_seconds'] == 1.5
    assert receipt['sample_rate'] == 48000 and receipt['channels'] == 2
    with wave.open(receipt['output'], 'rb') as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), '<i2').reshape(-1, 2)
    assert np.all(samples[0] == 0) and np.all(samples[-20000:] == 0)
    assert 3500 < np.max(np.abs(samples)) < 5000
    assert receipt['report']['clipped_samples'] == 0
    assert backend.audio_edit_tasks.read_receipt(task['payload']) == receipt
    assert client.get(ROOT + '/plan').json() == before
    assert backend.require_project_manifest('preset-test')['assets'][-1]['id'] == 'audio-edit-' + task['id']
    async def verify():
        async with Client(create_server('http://localhost', httpx.ASGITransport(app=backend.app))) as agent:
            result = (await agent.call_tool('submit_audio_edit', {'project_id': 'preset-test', **body})).structured_content
            assert result['ok'] and result['data']['id'] == task['id']
    asyncio.run(verify())
    (project / 'audio-browser-result.json').write_text(json.dumps(finished, ensure_ascii=False), encoding='utf-8')


def test_explicit_audio_selection_long_trim_and_short_rejection(tmp_path):
    source = wav(tmp_path / 'long.wav', seconds=2)
    with pytest.raises(ValueError, match='exceeds available'):
        audio_edit.edit_audio(source, tmp_path / 'rejected.wav', duration_seconds=3, output_root=tmp_path)
    assert not (tmp_path / 'rejected.wav').exists()
    result = audio_edit.edit_audio(source, tmp_path / 'trim.wav', start_seconds=.5, duration_seconds=.75,
                                          gain=.5, fade_in_seconds=.1, fade_out_seconds=.1, output_root=tmp_path)
    assert result['frames'] == 36000 and result['duration_seconds'] == .75
    with wave.open(result['output'], 'rb') as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), '<i2')
    assert samples[0] == samples[-1] == 0 and np.max(np.abs(samples)) <= 4096


def test_candidate_differences_prefer_executed_seed_and_original_recipe():
    current = {'seed': 42, 'workflow': 'older-frozen-path.json', 'duration_seconds': 5,
               'execution_snapshot': {'source_workflow': 'original-recipe.json', 'parameters': {'seed': 57, 'duration': 7.3}}}
    old = {'seed': 42, 'workflow': 'another-frozen-path.json', 'duration_seconds': 5,
           'execution_snapshot': {'source_workflow': 'original-recipe.json', 'parameters': {'seed': 42, 'duration': 5}}}
    assert creative_inspection.version_differences(current, old) == [
        {'field': 'duration_seconds', 'current': 7.3, 'previous': 5}, {'field': 'seed', 'current': 57, 'previous': 42}]


def test_mute_and_master_paths_are_exclusive_and_mp3_duration_is_actual(edit_client):
    client, root, workspace, plan_path = edit_client
    source = wav(workspace / 'long.wav', seconds=11)
    mp3 = workspace / 'master.mp3'
    ffmpeg('-i', source, '-c:a', 'libmp3lame', mp3)
    register('edit-test', 'mp3-master', mp3, 'audio')
    spec = importlib.util.spec_from_file_location('builder_test', BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    before = client.get(root + '/plan').json()
    master = client.put(root + '/assembly/sound', json={'expected_revision': before['revision'], 'policy': 'complete_master', 'audio_asset_id': 'mp3-master'})
    assert master.status_code == 200, master.text
    assert 11 <= master.json()['sound']['duration_seconds'] < 11.1
    document, _ = backend.load_project_plan('edit-test')
    original = builder.build_graph(document, str(mp3), 'isolated')
    assert original['concatenate']['inputs']['complete_audio'] == ['load-complete-audio', 0]
    rejected = client.put(root + '/assembly/sound', json={'expected_revision': document['revision'], 'policy': 'mute', 'audio_asset_id': 'mp3-master'})
    assert rejected.status_code == 422
    assert client.get(root + '/plan').json() == document
    muted = client.put(root + '/assembly/sound', json={'expected_revision': document['revision'], 'policy': 'mute'})
    assert muted.status_code == 200, muted.text
    document, _ = backend.load_project_plan('edit-test')
    document['segments'][0]['audio'] = {'delivery_master': 'unavailable-old-master.wav'}
    graph = builder.build_graph(document, str(mp3), 'isolated')
    assert all('audio' not in node['inputs'] for node in graph.values() if node['class_type'] == 'CreateVideo')
    assert all('audio' not in node['inputs'] for node in graph.values() if node['class_type'] == 'MiniMaxH3OutputTrimT8')
    assert not any(node['class_type'] == 'LoadAudio' for node in graph.values())
    assert 'complete_audio' not in graph['concatenate']['inputs']
    assert source.is_file() and mp3.is_file()
    async def verify():
        async with Client(create_server('http://localhost', httpx.ASGITransport(app=backend.app))) as agent:
            data = (await agent.call_tool('read_assembly_edit', {'project_id': 'edit-test'})).structured_content
            assert data['data'] == client.get(root + '/assembly/edit').json()
            data = (await agent.call_tool('set_assembly_sound', {'project_id': 'edit-test', 'expected_revision': document['revision'], 'policy': 'segment_native'})).structured_content
            assert data['ok'] and data['data']['sound']['policy'] == 'segment_native'
    asyncio.run(verify())
