"""Explicit audio loudness analysis and normalization candidates.

CPU-only checks use a generated PCM16 WAV; tests that invoke the real local
FFmpeg are marked ``host_tool``. The router is mounted on an isolated FastAPI
app so this file does not depend on app.py wiring order, while project state and
manifest helpers stay the real ones created by ``preset_client``.
"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend import audio_normalization
from backend.routers import audio_delivery


BASE = '/api/projects/preset-test'


def _write_tone(path: Path, *, seconds: float = 1.0, rate: int = 8000, frequency: float = 220.0) -> None:
    frames = int(rate * seconds)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        data = bytearray()
        for index in range(frames):
            sample = int(0.25 * 32767 * math.sin(2 * math.pi * frequency * index / rate))
            data += sample.to_bytes(2, 'little', signed=True)
        stream.writeframes(bytes(data))


def register_audio(project: Path, *, asset_id: str = 'voice', suffix: str = '.wav') -> Path:
    path = project / f'{asset_id}{suffix}'
    if suffix == '.wav':
        _write_tone(path)
    else:
        path.write_bytes(b'not audio')
    manifest = backend.require_project_manifest('preset-test')
    manifest.setdefault('assets', []).append({
        'id': asset_id, 'kind': 'audio', 'ready': True, 'title': '试音',
        'sources': {'A': str(path)},
    })
    backend.save_project_manifest(manifest)
    return path


@pytest.fixture()
def audio_client(preset_client):
    backend_client, project = preset_client
    app = FastAPI()
    app.include_router(audio_delivery.ROUTER)
    return backend_client, TestClient(app), project


def _manifest_bytes(project_id: str = 'preset-test') -> bytes:
    path = Path(backend.require_project_manifest(project_id)['_manifest_path'])
    return path.read_bytes()


def test_tool_status_reports_missing_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_normalization.media_operations, 'FFMPEG', tmp_path / 'missing-ffmpeg.exe')
    status = audio_normalization.tool_status()
    assert status['available'] is False
    assert status['method'] == 'ffmpeg_loudnorm'
    assert status['missing'] == [str(tmp_path / 'missing-ffmpeg.exe')]


def test_target_parameters_are_explicit_and_bounded():
    assert audio_normalization.validate_target_parameters(-16, -1) == {'target_lufs': -16.0, 'true_peak_db': -1.0}
    for value in (True, -25, -7, float('nan'), float('inf'), 'x', None):
        with pytest.raises(ValueError):
            audio_normalization.validate_target_parameters(value, -1)
    for value in (True, -4, 1, float('nan'), 'x', None):
        with pytest.raises(ValueError):
            audio_normalization.validate_target_parameters(-16, value)


def test_loudnorm_parser_handles_silence_and_rejects_missing_measurement():
    assert audio_normalization._metric('-inf', 'input_i') == -120.0
    assert audio_normalization._metric('-Infinity', 'input_lra') == -120.0
    with pytest.raises(audio_normalization.AudioAnalysisFailed):
        audio_normalization._parse_loudnorm_json('loudnorm did not print JSON')
    measured = audio_normalization._parse_loudnorm_json(
        'noise {not json} tail {"input_i": "-18.2", "input_lra": "0.0", "input_tp": "-12.1"}')
    assert measured['input_i'] == '-18.2'


def test_analysis_requires_explicit_binding_and_registered_source(audio_client):
    backend_client, client, project = audio_client
    source = register_audio(project)
    foreign = backend.blank_project_manifest('foreign', 'series', 'foreign', project / 'foreign')
    foreign['assets'] = [{'id': 'foreign-voice', 'kind': 'audio', 'ready': True, 'sources': {'A': str(source)}}]
    backend.save_project_manifest(foreign)
    register_audio(project, asset_id='notes', suffix='.txt')
    before = _manifest_bytes()

    missing = client.get(BASE + '/audio/analysis')
    assert missing.status_code == 422

    unknown_binding = client.get(BASE + '/audio/analysis', params={'asset_id': 'NOPE', 'source_asset_id': 'voice'})
    assert unknown_binding.status_code == 422
    assert unknown_binding.json()['detail']['code'] == 'invalid_audio_binding'

    unknown_source = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'missing'})
    assert unknown_source.status_code == 422
    assert unknown_source.json()['detail']['code'] == 'invalid_audio_source'

    foreign_source = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'foreign-voice'})
    assert foreign_source.status_code == 422
    assert foreign_source.json()['detail']['code'] == 'invalid_audio_source'

    unsupported = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'notes'})
    assert unsupported.status_code == 422
    assert unsupported.json()['detail']['code'] == 'invalid_audio_source'
    assert _manifest_bytes() == before


def test_missing_ffmpeg_returns_structured_blocker_for_both_routes(audio_client, monkeypatch, tmp_path):
    _, client, project = audio_client
    register_audio(project)
    missing = tmp_path / 'missing-ffmpeg.exe'
    monkeypatch.setattr(audio_normalization.media_operations, 'FFMPEG', missing)
    before = _manifest_bytes()

    analysis = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'voice'})
    assert analysis.status_code == 503, analysis.text
    detail = analysis.json()['detail']
    assert detail['code'] == 'audio_tool_unavailable'
    assert detail['missing'] == [str(missing)]
    assert detail['method'] == 'ffmpeg_loudnorm'

    normalized = client.post(BASE + '/audio/normalize', json={
        'asset_id': 'MASTER', 'source_asset_id': 'voice', 'target_lufs': -16, 'true_peak_db': -1,
    })
    assert normalized.status_code == 503, normalized.text
    assert normalized.json()['detail']['code'] == 'audio_tool_unavailable'
    assert _manifest_bytes() == before
    workspace = backend.resolve_registered_path(str(backend.require_project_manifest('preset-test')['workspace_root']))
    assert not (workspace / 'audio-normalization').exists()


def test_normalize_rejects_invalid_parameters_without_writes(audio_client, monkeypatch):
    _, client, project = audio_client
    register_audio(project)
    register_audio(project, asset_id='second')
    before = _manifest_bytes()
    valid = {'asset_id': 'MASTER', 'source_asset_id': 'voice', 'target_lufs': -16, 'true_peak_db': -1}

    for change in ({'target_lufs': -7}, {'target_lufs': True}, {'true_peak_db': -4}, {'true_peak_db': 0.5},
                   {'asset_id': 'NOPE'}, {'source_asset_id': 'foreign-voice'},
                   {'unexpected': 1}, {'idempotency_key': 'x' * 200}):
        response = client.post(BASE + '/audio/normalize', json={**valid, **change})
        assert response.status_code == 422, (change, response.text)
    misspelled = client.post(BASE + '/audio/normalize', json={'asset_id': 'MASTER', 'source_asset_id': 'voice'})
    assert misspelled.status_code == 422
    assert _manifest_bytes() == before


def test_normalize_failure_does_not_register_candidate(audio_client, monkeypatch):
    _, client, project = audio_client
    register_audio(project)
    before = _manifest_bytes()

    def fail(*args, **kwargs):
        raise audio_normalization.AudioNormalizationFailed('模拟 FFmpeg 失败')

    monkeypatch.setattr(audio_normalization, 'normalize', fail)
    response = client.post(BASE + '/audio/normalize', json={
        'asset_id': 'MASTER', 'source_asset_id': 'voice', 'target_lufs': -16, 'true_peak_db': -1,
    })
    assert response.status_code == 502, response.text
    assert response.json()['detail']['code'] == 'audio_normalization_failed'
    assert _manifest_bytes() == before

    def fail_analysis(*args, **kwargs):
        raise audio_normalization.AudioAnalysisFailed('模拟分析失败')

    monkeypatch.setattr(audio_normalization, 'analyze', fail_analysis)
    analysis = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'voice'})
    assert analysis.status_code == 502, analysis.text
    assert analysis.json()['detail']['code'] == 'audio_analysis_failed'
    assert _manifest_bytes() == before


def test_normalize_rejects_output_outside_trusted_root(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_normalization, 'tool_status', lambda: {
        'available': True, 'method': 'ffmpeg_loudnorm', 'ffmpeg': 'ffmpeg', 'missing': [],
    })
    source = tmp_path / 'source.wav'
    _write_tone(source)
    with pytest.raises(ValueError, match='作品目录'):
        audio_normalization.normalize(source, tmp_path / 'outside.wav',
                                      target_lufs=-16, true_peak_db=-1,
                                      output_root=tmp_path / 'workspace')
    assert not (tmp_path / 'outside.wav').exists()


def test_measure_timeout_maps_to_analysis_failure(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise audio_normalization.AudioNormalizationFailed('模拟执行超时')

    monkeypatch.setattr(audio_normalization, '_run_ffmpeg', fail)
    with pytest.raises(audio_normalization.AudioAnalysisFailed):
        audio_normalization._measure(tmp_path / 'x.wav')


@pytest.mark.host_tool
def test_analysis_measures_integrated_lufs_true_peak_and_lra(audio_client):
    _, client, project = audio_client
    register_audio(project)
    before = _manifest_bytes()

    response = client.get(BASE + '/audio/analysis', params={'asset_id': 'MASTER', 'source_asset_id': 'voice'})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['available'] is True
    assert body['method'] == 'ffmpeg_loudnorm'
    assert body['asset_id'] == 'MASTER' and body['binding'] == {'asset_id': 'MASTER', 'kind': 'assembly'}
    assert body['asset_id'] == 'MASTER'
    assert body['source']['sample_rate'] == 8000 and body['source']['channels'] == 1
    measurement = body['measurement']
    assert -40 < measurement['integrated_lufs'] < -6
    assert -30 < measurement['true_peak_db'] < 0
    assert 0 <= measurement['lra'] <= 100
    assert isinstance(measurement['silence_metrics'], list)
    assert measurement['method'] == 'ffmpeg_loudnorm'
    assert _manifest_bytes() == before


@pytest.mark.host_tool
def test_normalize_creates_candidate_bound_to_segment_then_master(audio_client):
    backend_client, client, project = audio_client
    register_audio(project)
    created = backend_client.post(BASE + '/plan/segments', json={
        'segment_id': 'S01', 'duration_seconds': 1, 'workflow': 'workflows/normalize.json',
    })
    assert created.status_code == 200, created.text
    manifest = backend.require_project_manifest('preset-test')
    plan, plan_path = backend.load_project_plan('preset-test')
    plan['assembly'] = {'status': 'candidate_generated', 'output': 'output/old.mp4', 'prompt_id': 'old-task'}
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    plan_before = plan_path.read_bytes()
    source = project / 'voice.wav'
    source_before = source.read_bytes()

    response = client.post(BASE + '/audio/normalize', json={
        'asset_id': 'S01', 'source_asset_id': 'voice', 'target_lufs': -20, 'true_peak_db': -2,
        'idempotency_key': 'normalize-once',
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['binding'] == {'asset_id': 'S01', 'kind': 'segment'}
    assert body['target'] == {'target_lufs': -20.0, 'true_peak_db': -2.0}
    assert body['method'] == 'ffmpeg_loudnorm'
    assert body['adopted'] is False and body['reused'] is False
    assert body['candidate_asset_id'].startswith('audio-normalization-')
    assert plan_path.read_bytes() == plan_before
    assert source.read_bytes() == source_before

    candidate = next(asset for asset in backend.require_project_manifest('preset-test')['assets']
                     if asset.get('id') == body['candidate_asset_id'])
    assert candidate['kind'] == 'audio' and candidate['ready'] is True
    assert candidate['audio_normalization']['bound_asset_id'] == 'S01'
    assert candidate['audio_normalization']['binding_kind'] == 'segment'
    assert candidate['audio_normalization']['source_asset_id'] == 'voice'
    assert candidate['audio_normalization']['method'] == 'ffmpeg_loudnorm'
    assert candidate['audio_normalization']['target_lufs'] == -20.0
    assert candidate['audio_normalization']['true_peak_db'] == -2.0
    output = backend.resolve_registered_path(candidate['sources']['A'])
    assert output.is_file() and output != source
    with wave.open(str(output), 'rb') as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (8000, 1, 2)
        assert stream.getnframes() > 0
    report = candidate['audio_normalization']['report']
    assert 'integrated_lufs' in report['input_measurement']
    assert 'integrated_lufs' in report['output_measurement']
    assert report['parameters'] == {'target_lufs': -20.0, 'true_peak_db': -2.0}

    repeated = client.post(BASE + '/audio/normalize', json={
        'asset_id': 'S01', 'source_asset_id': 'voice', 'target_lufs': -20, 'true_peak_db': -2,
        'idempotency_key': 'normalize-once',
    })
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['reused'] is True
    assert repeated.json()['candidate_asset_id'] == body['candidate_asset_id']
    assert plan_path.read_bytes() == plan_before

    master = client.post(BASE + '/audio/normalize', json={
        'asset_id': 'MASTER', 'source_asset_id': 'voice', 'target_lufs': -18, 'true_peak_db': -1,
    })
    assert master.status_code == 200, master.text
    assert master.json()['binding'] == {'asset_id': 'MASTER', 'kind': 'assembly'}
    assert master.json()['candidate_asset_id'] != body['candidate_asset_id']
    assert plan_path.read_bytes() == plan_before
