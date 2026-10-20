"""Video-audio extraction into a registered project audio asset.

The contract tests use the real project/manifest/adoption helpers but stub the
media probe and FFmpeg extraction, so the fast layer stays independent of host
tools. One real encode/extract is marked ``host_tool``.

Pinned behaviour:

* only an explicitly adopted candidate of the selected slot may be read;
* a video without an audio track is rejected before any write;
* the new asset is a *new* PCM16 WAV registered as ``kind=audio`` with
  ``sources.A``; the source, plan, adoption and assembly stay untouched;
* loudness normalization, assembly replacement and model downloads never happen;
* missing FFmpeg is a structured blocker;
* replaying the same request returns the original asset instead of duplicating.
"""
from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import pytest

from backend import app as backend
from backend import audio_asset
from backend.media_operations import FFMPEG
from backend.routers.audio_asset import ROUTER

BASE = '/api/projects/preset-test'


def _mount(app) -> None:
    """Mount the router for this test, ahead of the trailing ``/`` static mount.

    Production wiring adds the same ``include_router`` before app.py's final
    static mount; appending after that mount would make the routes unreachable.
    """
    if any(getattr(route, 'original_router', None) is ROUTER for route in app.routes):
        return
    app.include_router(ROUTER)
    added = app.routes.pop()
    static_mount = next((index for index, route in enumerate(app.routes)
                         if getattr(route, 'path', None) in ('/', '')), len(app.routes))
    app.router.routes.insert(static_mount, added)


@pytest.fixture()
def audio_client(preset_client):
    client, project = preset_client
    _mount(backend.app)
    return client, project


def _write_plan(project: Path, source: Path, *, segment_id: str = 'S01') -> None:
    document, plan_path = backend.load_project_plan('preset-test')
    document.update({
        'revision': 1,
        'segments': [{
            'id': segment_id, 'start_seconds': 0, 'duration_seconds': 1.0,
            'status': 'video_generated', 'current_version_task_id': 'task-v1',
            'comfyui_task_id': 'prompt-1',
            'video': {'path': str(source), 'variant': 'v3-A'},
        }],
        'assembly': None,
    })
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')


def _adopt(client, source: Path, *, segment_id: str = 'S01', stage: str = 'sample') -> None:
    response = client.post(BASE + '/reviews', json={
        'asset_id': segment_id, 'stage': stage, 'status': 'approved',
        'note': '测试用明确采用记录', 'candidate_ref': str(source),
        'expected_revision': 0, 'expected_plan_revision': 1,
        'source': 'explicit-adoption', 'adopted_variant': 'A',
    })
    assert response.status_code == 200, response.text


def _source(project: Path, name: str = 'source.mp4') -> Path:
    path = project / 'workspaces' / 'preset-test' / 'assets' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'adopted-video-fixture')
    return path


def _write_wav(path: Path, *, seconds: float = 1.2, rate: int = 48000,
               channels: int = 2, frequency: float = 220.0, amplitude: float = .25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * seconds)
    data = bytearray()
    for index in range(frames):
        sample = int(amplitude * 32767 * math.sin(2 * math.pi * frequency * index / rate))
        for _ in range(channels):
            data += sample.to_bytes(2, 'little', signed=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(data))


def _stub_media(monkeypatch, *, audio: bool = True, duration: float = 1.2,
                sample_rate: int = 48000, channels: int = 2) -> dict:
    facts = {
        'known': True, 'audio_streams': 1 if audio else 0,
        'duration_seconds': duration,
        'sample_rate': sample_rate if audio else None,
        'channels': channels if audio else None,
        'width': 640, 'height': 360,
    }
    calls = []

    def probe(path):
        calls.append(('probe', Path(path)))
        return dict(facts)

    def extract(source, output, *, sample_rate, channels):
        calls.append(('extract', Path(source), Path(output)))
        _write_wav(output, seconds=duration, rate=sample_rate or 48000, channels=channels or 2)

    monkeypatch.setattr(audio_asset, 'probe_audio', probe)
    monkeypatch.setattr(audio_asset, 'extract_track', extract)
    return {'facts': facts, 'calls': calls}


def _manifest_bytes(project_id: str = 'preset-test') -> bytes:
    return Path(backend.require_project_manifest(project_id)['_manifest_path']).read_bytes()


def _asset(project_id: str, asset_id: str) -> dict:
    return next(item for item in backend.require_project_manifest(project_id)['assets']
                if item.get('id') == asset_id)


def test_extract_registers_one_new_audio_asset_and_replays_idempotently(audio_client, monkeypatch):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _adopt(client, source)
    stub = _stub_media(monkeypatch)
    source_bytes = source.read_bytes()
    plan_before = client.get(BASE + '/plan').json()
    manifest_before = _manifest_bytes()

    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 200, response.text
    body = response.json()
    asset_id = body['asset_id']
    assert asset_id.startswith('audio-extract-')
    assert body['media_ref'] == f'asset:{asset_id}:A'
    assert body['reused'] is False
    extraction = body['extraction']
    assert extraction['method'] == 'ffmpeg_pcm_s16le'
    assert 1.1 <= extraction['duration_seconds'] <= 1.3
    assert extraction['sample_rate'] == 48000
    assert extraction['channels'] == 2
    assert .2 < extraction['peak'] < .3
    assert extraction['peak_dbfs'] < -6
    assert extraction['peak_metric'] == 'sample_peak_dbfs'
    assert extraction['format_conversion']['loudness_normalized'] is False
    assert '未做响度归一' in body['note']
    assert [call[0] for call in stub['calls']] == ['probe', 'extract']

    asset = _asset('preset-test', asset_id)
    assert asset['kind'] == 'audio' and asset['ready'] is True
    assert asset['sources']['A'] == body['audio_asset']['sources']['A']
    output = backend.resolve_registered_path(asset['sources']['A'])
    assert output.is_file() and output != source and output.suffix.lower() == '.wav'
    assert output.parent.name == 'audio-extract'
    with wave.open(str(output), 'rb') as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (48000, 2, 2)
        assert stream.getnframes() > 0
    record = asset['audio_extract']
    assert record['source_asset_id'] == 'S01' and record['source_slot'] == 'sample'
    assert record['source_candidate_ref'] == str(source)
    assert record['loudness_normalized'] is False
    assert record['assembly_modified'] is False and record['model_downloaded'] is False
    # The registered asset is consumable by the existing audio analysis/normalize entry.
    resolved, resolved_record = backend.audio_edit_source(backend.require_project_manifest('preset-test'), asset_id)
    assert resolved == output and resolved_record['asset_id'] == asset_id

    # The adopted source, plan, assembly and adoption records are untouched.
    assert source.read_bytes() == source_bytes
    assert client.get(BASE + '/plan').json() == plan_before
    assert client.get(BASE + '/tasks').json()['tasks'] == []
    assert _asset('preset-test', asset_id) is not None

    repeated = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['reused'] is True
    assert repeated.json()['asset_id'] == asset_id
    assert len([item for item in backend.require_project_manifest('preset-test')['assets']
                if item.get('id') == asset_id]) == 1
    assert _manifest_bytes() != manifest_before  # only the new asset was appended

    listed = client.get(BASE + '/audio/assets')
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    row = next(item for item in payload['audio_assets'] if item['asset_id'] == asset_id)
    assert row['media_ref'] == f'asset:{asset_id}:A'
    assert row['extraction']['source_asset_id'] == 'S01'
    assert row['extraction']['duration_seconds'] == extraction['duration_seconds']
    assert row['source'] == asset['sources']['A']
    assert payload['total'] == len(payload['audio_assets'])


def test_extract_rejects_a_source_without_an_audio_track(audio_client, monkeypatch):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _adopt(client, source)
    _stub_media(monkeypatch, audio=False)
    before = _manifest_bytes()

    def fail(*args, **kwargs):
        pytest.fail('extraction ran for a source without audio')

    monkeypatch.setattr(audio_asset, 'extract_track', fail)
    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 422, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'source_has_no_audio'
    assert detail['source_asset_id'] == 'S01' and detail['source_slot'] == 'sample'
    assert _manifest_bytes() == before
    assert not (project / 'workspaces' / 'preset-test' / 'audio-extract').exists()


def test_list_audio_assets_includes_existing_non_extraction_audio(audio_client):
    client, project = audio_client
    path = project / 'workspaces/preset-test/assets/voice.wav'
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(path)
    manifest = backend.require_project_manifest('preset-test')
    manifest.setdefault('assets', []).append({
        'id': 'voice', 'kind': 'audio', 'title': '旧音频', 'ready': True,
        'origin': '上传', 'sources': {'A': str(path)},
    })
    backend.save_project_manifest(manifest)

    response = client.get(BASE + '/audio/assets')
    assert response.status_code == 200, response.text
    rows = {row['asset_id']: row for row in response.json()['audio_assets']}
    assert rows['voice']['media_ref'] == 'asset:voice:A'
    assert rows['voice']['extraction'] is None


def test_extract_requires_an_explicitly_adopted_slot(audio_client, monkeypatch):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _stub_media(monkeypatch)
    before = _manifest_bytes()

    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'source_not_adopted'
    assert _manifest_bytes() == before

    unknown = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'NOPE', 'source_slot': 'sample'})
    assert unknown.status_code == 409
    assert unknown.json()['detail']['code'] == 'source_not_adopted'

    bad_slot = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'auto'})
    assert bad_slot.status_code == 422


def test_extract_rejects_a_candidate_changed_after_adoption(audio_client, monkeypatch):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _adopt(client, source)
    _stub_media(monkeypatch)
    source.write_bytes(b'adopted-video-fixture-changed')
    before = _manifest_bytes()

    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'adoption_candidate_changed'
    assert _manifest_bytes() == before


def test_extract_reports_missing_ffmpeg_as_a_structured_blocker(audio_client, monkeypatch, tmp_path):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _adopt(client, source)
    missing = tmp_path / 'missing-ffmpeg.exe'
    monkeypatch.setattr(audio_asset, 'FFMPEG', missing)
    before = _manifest_bytes()

    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 503, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'audio_tool_unavailable'
    assert detail['missing'] == [str(missing)]
    assert detail['method'] == 'ffmpeg_pcm_s16le'
    assert _manifest_bytes() == before


def test_extract_honours_explicit_asset_id_and_rejects_conflicts(audio_client, monkeypatch):
    client, project = audio_client
    source = _source(project)
    _write_plan(project, source)
    _adopt(client, source)
    _stub_media(monkeypatch)

    invalid = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'audio_asset_id': '../escape'})
    assert invalid.status_code == 422

    first = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'audio_asset_id': 'track-a'})
    assert first.status_code == 200, first.text
    assert first.json()['asset_id'] == 'track-a'
    assert first.json()['reused'] is False

    replay = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'audio_asset_id': 'track-a'})
    assert replay.status_code == 200
    assert replay.json()['reused'] is True and replay.json()['asset_id'] == 'track-a'

    renamed = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'audio_asset_id': 'track-b'})
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()['detail']['code'] == 'audio_asset_id_conflict'


def test_extraction_arguments_preserve_compatible_audio_and_never_add_loudnorm(tmp_path):
    source, output = tmp_path / 'source.mp4', tmp_path / 'out.wav'
    preserved = audio_asset.extraction_arguments(source, output, sample_rate=48000, channels=2)
    assert '-c:a' in preserved and preserved[preserved.index('-c:a') + 1] == 'pcm_s16le'
    assert '-vn' in preserved and '-map' in preserved
    assert '-ar' not in preserved and '-ac' not in preserved
    assert 'loudnorm' not in ' '.join(preserved)
    converted = audio_asset.extraction_arguments(source, output, sample_rate=192000, channels=6)
    assert converted[converted.index('-ar') + 1] == '48000'
    assert converted[converted.index('-ac') + 1] == '2'
    assert 'loudnorm' not in ' '.join(converted)


def test_extract_cross_user_project_is_not_reachable(private_workbench):
    a, b, _ownership, _project = private_workbench
    _mount(backend.app)
    created = a.post('/api/projects/create', json={
        'series': '隔离', 'title': '音轨', 'creation_mode': 'advanced'})
    assert created.status_code == 200, created.text
    project_id = created.json()['project']['id']

    other_extract = b.post(f'/api/projects/{project_id}/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert other_extract.status_code == 404, other_extract.text
    assert b.get(f'/api/projects/{project_id}/audio/assets').status_code == 404
    assert a.get(f'/api/projects/{project_id}/audio/assets').json()['audio_assets'] == []
    owner_attempt = a.post(f'/api/projects/{project_id}/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert owner_attempt.status_code == 409
    assert owner_attempt.json()['detail']['code'] == 'source_not_adopted'


def test_slot_paths_use_the_recorded_candidate_of_each_slot():
    manifest = {'assembly_asset_id': 'MASTER'}
    document = {
        'segments': [{'id': 'S01', 'video': {'path': 'sample.mp4',
                                             'finish_review': {'path': 'finish.mp4'}}}],
        'assembly': {'output': 'final.mp4'},
    }
    assert audio_asset._slot_path(document, manifest, 'S01', 'sample') == ('sample.mp4', 'MASTER')
    assert audio_asset._slot_path(document, manifest, 'S01', 'finish') == ('finish.mp4', 'MASTER')
    assert audio_asset._slot_path(document, manifest, 'MASTER', 'final') == ('final.mp4', 'MASTER')
    with pytest.raises(audio_asset.AudioAssetError) as mismatch:
        audio_asset._slot_path(document, manifest, 'S01', 'final')
    assert mismatch.value.code == 'assembly_source_mismatch'
    with pytest.raises(audio_asset.AudioAssetError) as missing:
        audio_asset._slot_path({'segments': []}, manifest, 'S02', 'sample')
    assert missing.value.code == 'segment_not_found'


def _make_video(path: Path, *, width: int = 320, height: int = 240, seconds: float = 1.0) -> None:
    if not FFMPEG.is_file():
        pytest.skip('local FFmpeg is required for the host-tool fixture')
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(FFMPEG), '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'lavfi', '-i', f'testsrc=size={width}x{height}:rate=12',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
               '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)]
    log = path.with_suffix('.ffmpeg.log')
    with log.open('wb') as handle:
        result = subprocess.run(command, stdout=handle, stderr=handle, timeout=120)
    assert result.returncode == 0, log.read_text(encoding='utf-8', errors='replace')[-800:]
    assert path.is_file() and path.stat().st_size > 0


@pytest.mark.host_tool
def test_extract_real_video_audio_track_into_pcm16_wav(audio_client):
    client, project = audio_client
    source = project / 'workspaces' / 'preset-test' / 'assets' / 'real-source.mp4'
    _make_video(source)
    _write_plan(project, source)
    _adopt(client, source)
    source_stat = source.stat()

    response = client.post(BASE + '/audio/extract', json={
        'source_asset_id': 'S01', 'source_slot': 'sample'})
    assert response.status_code == 200, response.text
    body = response.json()
    extraction = body['extraction']
    assert extraction['sample_rate'] == 48000
    assert extraction['channels'] in (1, 2)
    assert .8 <= extraction['duration_seconds'] <= 1.2
    assert 0 < extraction['peak'] <= 1
    output = backend.resolve_registered_path(_asset('preset-test', body['asset_id'])['sources']['A'])
    assert output.is_file() and output.suffix.lower() == '.wav'
    with wave.open(str(output), 'rb') as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (
            48000, extraction['channels'], 2)
        assert stream.getnframes() > 0
    assert (source.stat().st_size, source.stat().st_mtime_ns) == (source_stat.st_size, source_stat.st_mtime_ns)
    assert client.get(BASE + '/plan').json()['assembly'] is None
    assert client.get(BASE + '/tasks').json()['tasks'] == []
