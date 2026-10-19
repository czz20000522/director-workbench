from __future__ import annotations

import json
from pathlib import Path

from backend import app as backend


def _facts() -> dict:
    return {
        'duration_seconds': 12.0, 'width': 640, 'height': 360, 'fps': 24.0,
        'has_video': True, 'has_audio': True, 'audio_channels': 2, 'sample_rate': 48000,
    }


def _fake_ffmpeg(monkeypatch):
    calls = []

    def run(ffmpeg, args):
        calls.append(args)
        Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(args[-1]).write_bytes(b'fixture')

    monkeypatch.setattr('tools.reference_decompose.run_ffmpeg', run)
    return calls


def test_upload_registers_facts_without_split_or_asr_task(reverse_client, monkeypatch, tmp_path):
    client, _, _ = reverse_client
    monkeypatch.setattr('tools.reference_decompose.media_facts', lambda source: _facts())
    before = backend.db()
    with before as connection:
        count_before = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    response = client.post('/api/projects/project-a/reverse-analyses', files={'file': ('reference.mp4', b'video', 'video/mp4')})
    assert response.status_code == 202, response.text
    analysis = response.json()
    assert analysis['status'] == 'registered'
    assert analysis['source']['registered'] is True
    assert analysis['timeline'] == []
    assert analysis['observations'] == []
    assert 'import_task_id' not in analysis
    with backend.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == count_before
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE payload LIKE '%reference_decomposition%'").fetchone()[0] == 0


def test_explicit_range_and_sampling_create_observations_only(reverse_client, monkeypatch):
    client, _, analysis_path = reverse_client
    monkeypatch.setattr(backend, 'resolve_registered_path', lambda value: Path(value))
    source_path = analysis_path.parent / 'source' / 'reference.mp4'
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b'video')
    document = json.loads(analysis_path.read_text(encoding='utf-8'))
    document['source'] = {'path': str(source_path), **_facts()}
    analysis_path.write_text(json.dumps(document), encoding='utf-8')
    calls = _fake_ffmpeg(monkeypatch)
    endpoint = '/api/projects/project-a/reverse-analyses/ref-test'
    ranged = client.post(endpoint + '/observations/range', json={'start_seconds': 2, 'end_seconds': 5, 'frame_seconds': 3})
    assert ranged.status_code == 201, ranged.text
    observation = ranged.json()['observation']
    assert observation['start_seconds'] == 2 and observation['end_seconds'] == 5
    assert observation['is_shot_cut'] is False
    sampled = client.post(endpoint + '/observations/sample', json={'times_seconds': [1, 7]})
    assert sampled.status_code == 201, sampled.text
    assert sampled.json()['observation']['review_label'] == '等距采样/待确认范围'
    assert len(calls) == 4  # range + frame + two sampled frames
    latest = client.get(endpoint).json()
    assert len(latest['timeline']) == 3
    assert all(item['is_shot_cut'] is False for item in latest['timeline'][1:])


def test_empty_explicit_conversion_does_not_write_plan(reverse_client):
    client, manifest, _ = reverse_client
    plan_path = Path(manifest['plan_path'])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({'revision': 0, 'segments': []}), encoding='utf-8')
    before = plan_path.read_bytes()
    response = client.post('/api/projects/project-a/reverse-analyses/ref-test/convert', json={'expected_revision': 0, 'shots': []})
    assert response.status_code == 422
    assert plan_path.read_bytes() == before


def test_explicit_asr_is_model_candidate_and_keeps_review_boundary(reverse_client, monkeypatch):
    client, _, analysis_path = reverse_client
    monkeypatch.setattr(backend, 'resolve_registered_path', lambda value: Path(value))
    source_path = analysis_path.parent / 'source' / 'reference.wav'
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b'audio')
    document = json.loads(analysis_path.read_text(encoding='utf-8'))
    document['source'] = {'path': str(source_path), **_facts()}
    analysis_path.write_text(json.dumps(document), encoding='utf-8')
    audio_path = analysis_path.parent / 'observations' / 'asr' / 'reference-asr.wav'
    def extract_audio(source, output):
        output.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b'wav')
        return audio_path
    monkeypatch.setattr('tools.reference_decompose.extract_audio_for_asr', extract_audio)
    monkeypatch.setattr('tools.reference_decompose.transcribe_audio', lambda audio, output: ({'segments': [{'start': 0, 'end': 1, 'text': '候选'}]}, None))
    response = client.post('/api/projects/project-a/reverse-analyses/ref-test/observations/asr', json={})
    assert response.status_code == 201, response.text
    latest = response.json()['analysis']
    assert latest['artifacts']['transcript']['status'] == 'candidate_needs_review'
    assert latest['artifacts']['transcript']['provenance'] == 'model_inference'
    assert latest['artifacts']['transcript']['user_status'] == 'unreviewed'


def test_conversion_requires_explicit_fields_and_keeps_model_observation_out_of_prompt(reverse_client):
    client, manifest, path = reverse_client
    plan_path = Path(manifest['plan_path'])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({'revision': 0, 'segments': []}), encoding='utf-8')
    revision = client.get('/api/projects/project-a/reverse-analyses/ref-test').json().get('revision', 0)
    response = client.post('/api/projects/project-a/reverse-analyses/ref-test/convert', json={
        'expected_revision': revision,
        'shots': [{'segment_id': 'caller-shot', 'start_seconds': 1, 'end_seconds': 4,
                   'fields': {'performance': '演员回头', 'prompt': '夜店中回头'}}],
    })
    assert response.status_code == 200, response.text
    segment = response.json()['created_segments'][0]
    assert segment['performance'] == '演员回头'
    assert segment['prompt'] == '夜店中回头'
    assert segment['source_range_seconds'] == [1.0, 4.0]
    assert segment['source_observation'] == ''
    assert json.loads(path.read_text(encoding='utf-8'))['status'] == 'converted_to_production'
