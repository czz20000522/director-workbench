"""情绪控制：只放行这台机器上真实可用的 IndexTTS 情绪路径。"""
import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import dubbing, emotion, speech

TEXT_READY = {'text_engine_ready': True}


def test_none_mode_sends_no_arguments():
    assert emotion.validate(None, **TEXT_READY) == {'mode': 'none'}
    assert emotion.validate({'mode': 'none'}, **TEXT_READY) == {'mode': 'none'}
    assert emotion.validate({'mode': 'none', 'alpha': None, 'vector': None}, **TEXT_READY) == {'mode': 'none'}
    assert emotion.infer_arguments({'mode': 'none'}) == {}
    with pytest.raises(ValueError, match='其他情绪字段'):
        emotion.validate({'mode': 'none', 'alpha': 0.5}, **TEXT_READY)


def test_reference_mode_requires_a_reference_and_blends_by_alpha(tmp_path):
    report = emotion.validate({'mode': 'reference', 'reference': 'D:/voice.wav', 'alpha': 0.4}, **TEXT_READY)
    assert report == {'mode': 'reference', 'use_qwen_emo': False, 'alpha': 0.4, 'reference': 'D:/voice.wav'}
    for missing in ({'mode': 'reference'}, {'mode': 'reference', 'reference': '  '}):
        with pytest.raises(ValueError, match='参考音频'):
            emotion.validate(missing, **TEXT_READY)
    with pytest.raises(ValueError, match='强度'):
        emotion.validate({'mode': 'reference', 'reference': 'a.wav', 'alpha': 1.5}, **TEXT_READY)
    source = tmp_path / 'emo.wav'
    source.write_bytes(b'emotion fixture')
    directory = tmp_path / 'task'
    directory.mkdir()
    frozen = emotion.freeze(emotion.validate({'mode': 'reference', 'reference': str(source), 'alpha': 0.4}, **TEXT_READY), directory)
    assert Path(frozen['reference']).is_file() and Path(frozen['reference']).parent == directory
    assert emotion.infer_arguments(frozen) == {'emo_audio_prompt': frozen['reference'], 'emo_alpha': 0.4}


def test_vector_mode_matches_the_official_webui_scaling():
    report = emotion.validate({'mode': 'vector', 'vector': [1] * 8}, **TEXT_READY)
    assert report['mode'] == 'vector'
    assert abs(sum(report['vector']) - 0.8) < 1e-4
    # 官方偏差：happy .9375 / angry .875 / sad 1 / afraid 1 / disgusted .9375 / melancholic .9375 / surprised .6875 / calm .5625
    scaled = emotion.normalize_vector([1, 1, 0, 0, 0, 0, 0, 0])
    assert scaled[0] == pytest.approx(0.9375 * 0.8 / 1.8125, abs=1e-6)
    assert emotion.infer_arguments(report)['emo_vector'] == report['vector']
    for bad in ([0] * 7, [0] * 9, [0.5] * 7 + [2], ['x'] * 8, None):
        with pytest.raises(ValueError):
            emotion.normalize_vector(bad)


def test_text_mode_needs_the_local_emotion_model():
    report = emotion.validate({'mode': 'text', 'text': '克制的悲伤'}, **TEXT_READY)
    assert report == {'mode': 'text', 'use_qwen_emo': True, 'text': '克制的悲伤'}
    assert emotion.infer_arguments(report) == {'use_emo_text': True, 'emo_text': '克制的悲伤'}
    with pytest.raises(ValueError, match='qwen0.6bemo4-merge'):
        emotion.validate({'mode': 'text', 'text': '克制的悲伤'}, text_engine_ready=False)
    for bad in ({'mode': 'text'}, {'mode': 'text', 'text': '   '}, {'mode': 'text', 'text': '情' * 201},
                {'mode': 'text', 'text': 'x', 'reference': 'a.wav'}):
        with pytest.raises(ValueError):
            emotion.validate(bad, **TEXT_READY)


def test_modes_are_reported_by_the_engine_capability(monkeypatch):
    monkeypatch.setattr(speech, 'MODEL_ROOT', Path('D:/definitely-absent'))
    assert speech.capability('indextts-2.5')['emotion_modes']['text'] is False
    monkeypatch.setattr(speech, 'MODEL_ROOT', Path('D:/absent-again'))
    assert dubbing.capability()['emotion_modes']['text'] is False


def _speech_payload(tmp_path, spec, task_id='emo-task', **kwargs):
    reference = tmp_path / 'voice.wav'
    reference.write_bytes(b'voice fixture')
    return speech.prepare(task_id, '天快黑了。', reference, tmp_path / 'speech',
                          engine=kwargs.pop('engine', 'indextts-2.5'), emotion_spec=spec, **kwargs)


def test_speech_prepare_freezes_emotion_and_rejects_auk(tmp_path, monkeypatch):
    model_root = tmp_path / 'models'
    (model_root / 'qwen0.6bemo4-merge').mkdir(parents=True)
    (model_root / 'qwen0.6bemo4-merge' / 'config.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(speech, 'MODEL_ROOT', model_root)
    emotion_source = tmp_path / 'emo.wav'
    emotion_source.write_bytes(b'emotion fixture')
    payload = _speech_payload(tmp_path, {'mode': 'reference', 'reference': str(emotion_source), 'alpha': 0.5})
    request = json.loads(Path(payload['request_path']).read_text(encoding='utf-8'))
    assert request['emotion']['mode'] == 'reference'
    assert Path(request['emotion']['reference']).is_file()
    assert Path(request['emotion']['reference']).parent == Path(payload['request_path']).parent
    assert _speech_payload(tmp_path, None, task_id='emo-none')['request']['emotion'] == {'mode': 'none'}
    vector = _speech_payload(tmp_path, {'mode': 'vector', 'vector': [0.5] * 8}, task_id='emo-vector')
    assert len(vector['request']['emotion']['vector']) == 8
    with pytest.raises(ValueError, match='AuK'):
        _speech_payload(tmp_path, {'mode': 'text', 'text': '悲伤'}, engine='auk-flash')
    with pytest.raises(ValueError, match='文本描述'):
        _speech_payload(tmp_path, {'mode': 'vector', 'vector': [0.5] * 8}, engine='voxcpm-2')
    with pytest.raises(ValueError, match='qwen0.6bemo4-merge'):
        monkeypatch.setattr(speech, 'MODEL_ROOT', tmp_path / 'absent')
        _speech_payload(tmp_path, {'mode': 'text', 'text': '悲伤'}, engine='indextts-2.5')


def test_emotion_belongs_to_the_submission_identity(preset_client, monkeypatch):
    client, project = preset_client
    model_root = project / 'emotion-model'
    (model_root / 'qwen0.6bemo4-merge').mkdir(parents=True)
    (model_root / 'qwen0.6bemo4-merge' / 'config.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(speech, 'MODEL_ROOT', model_root)
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    monkeypatch.setattr(speech, 'capability', lambda *args, **kwargs: {'available': True, 'missing': [], 'emotion_modes': {'none': True, 'reference': True, 'text': True, 'vector': True}})
    reference = project / 'voice.wav'
    reference.write_bytes(b'voice fixture')
    base = {'text': '天快黑了。', 'engine': 'indextts-2.5', 'voice_reference': str(reference), 'idempotency_key': 'emo-speech'}
    first = client.post('/api/projects/preset-test/speech-tasks', json={**base, 'emotion': {'mode': 'vector', 'vector': [0.5] * 8}})
    assert first.status_code == 202, first.text
    assert first.json()['payload']['request']['emotion']['mode'] == 'vector'
    assert client.post('/api/projects/preset-test/speech-tasks', json={**base, 'emotion': {'mode': 'vector', 'vector': [0.2] * 8}}).status_code == 409
    # Dropping the emotion is a different request, not a silent reuse of the old one.
    assert client.post('/api/projects/preset-test/speech-tasks', json=base).status_code == 409

    monkeypatch.setattr(dubbing, 'capability', lambda *args, **kwargs: {'available': True, 'missing': [], 'emotion_modes': {'none': True, 'reference': True, 'text': False, 'vector': True}})
    (project / 'shot.mp4').write_bytes(b'video fixture')
    manifest = backend.require_project_manifest('preset-test')
    manifest['assets'].append({'id': 'shot', 'kind': 'video', 'sources': {'A': str(project / 'shot.mp4')}})
    backend.save_project_manifest(manifest)
    monkeypatch.setattr(dubbing.creative_inspection, 'probe', lambda path: {
        'known': True, 'width': 576, 'height': 1024, 'duration_seconds': 6.0, 'fps': 24, 'audio_streams': 1})
    dub = {'source_asset_id': 'shot', 'voice_reference': str(reference), 'cues': [{'at_seconds': 1, 'text': '别回头'}],
           'idempotency_key': 'emo-dub'}
    accepted = client.post('/api/projects/preset-test/dubbing-tasks', json={**dub, 'emotion': {'mode': 'text', 'text': '压抑'}})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['payload']['request']['emotion']['text'] == '压抑'
    assert client.post('/api/projects/preset-test/dubbing-tasks', json={**dub, 'emotion': {'mode': 'text', 'text': '爆发'}}).status_code == 409
    assert client.post('/api/projects/preset-test/dubbing-tasks', json={**dub, 'idempotency_key': 'emo-dub-2',
                                                                       'emotion': {'mode': 'vector', 'vector': [0.5] * 9}}).status_code == 422


def test_dubbing_prepare_freezes_emotion_into_the_request(tmp_path, monkeypatch):
    model_root = tmp_path / 'models'
    model_root.mkdir()
    monkeypatch.setattr(speech, 'MODEL_ROOT', model_root)
    monkeypatch.setattr(dubbing.creative_inspection, 'probe', lambda path: {
        'known': True, 'width': 576, 'height': 1024, 'duration_seconds': 6.0, 'fps': 24, 'audio_streams': 1})
    source = tmp_path / 'shot.mp4'
    source.write_bytes(b'video fixture')
    reference = tmp_path / 'voice.wav'
    reference.write_bytes(b'voice fixture')
    emotion_source = tmp_path / 'emo.wav'
    emotion_source.write_bytes(b'emotion fixture')
    payload = dubbing.prepare('emo-dub', source, reference, [{'at_seconds': 0.5, 'text': '别回头', 'gain': 1}],
                              tmp_path / 'tasks', original_audio='keep', original_gain=1, engine='indextts-2.5',
                              emotion_spec={'mode': 'reference', 'reference': str(emotion_source), 'alpha': 0.6})
    request, _ = dubbing.frozen_request(payload)
    assert request['emotion']['mode'] == 'reference'
    assert Path(request['emotion']['reference']).is_file()
    plain = dubbing.prepare('emo-dub-2', source, reference, [{'at_seconds': 0.5, 'text': '别回头', 'gain': 1}],
                            tmp_path / 'tasks', original_audio='keep', original_gain=1, engine='indextts-2.5')
    assert dubbing.frozen_request(plain)[0]['emotion'] == {'mode': 'none'}
    with pytest.raises(ValueError, match='qwen0.6bemo4-merge'):
        dubbing.prepare('emo-dub-3', source, reference, [{'at_seconds': 0.5, 'text': '别回头', 'gain': 1}],
                        tmp_path / 'tasks', original_audio='keep', original_gain=1, engine='indextts-2.5',
                        emotion_spec={'mode': 'text', 'text': '压低声音'})
