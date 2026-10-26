"""命名设定库：显式保存、载入与删除，不替用户挑选参数。"""
import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import creative_settings

MUSIC = {'tags': '悬疑弦乐铺底', 'lyrics': '', 'duration_seconds': 24, 'bpm': 88, 'keyscale': 'A minor',
         'timesignature': '4', 'language': 'unknown', 'steps': 8, 'cfg': 1.0, 'shift': 3.0,
         'output_format': 'mp3', 'quality': '128k', 'generate_audio_codes': True, 'encode_cfg_scale': 2.0,
         'temperature': 0.85, 'top_p': 0.9, 'top_k': 0, 'min_p': 0.0}
DUBBING = {'engine': 'voxcpm-2', 'voice_reference': 'D:/voice.wav', 'seed': 20261025,
           'original_audio': 'keep', 'original_gain': 0.4}


def test_clean_name_rejects_unusable_labels():
    assert creative_settings.clean_name('  夜景追逐 88拍  ') == '夜景追逐 88拍'
    for name in ('', '   ', 'a' * 41, '...  ', 'a' * 100):
        with pytest.raises(ValueError):
            creative_settings.clean_name(name)


def test_validate_params_freezes_only_reusable_fields():
    music = creative_settings.validate_params('music', {**MUSIC, 'seed': 5, 'idempotency_key': 'x'})
    assert music == MUSIC and 'seed' not in music
    with pytest.raises(ValueError, match='时长'):
        creative_settings.validate_params('music', {**MUSIC, 'duration_seconds': 900})
    with pytest.raises(ValueError, match='缺少'):
        creative_settings.validate_params('music', {key: value for key, value in MUSIC.items() if key != 'bpm'})
    with pytest.raises(ValueError, match='未知参数'):
        creative_settings.validate_params('music', {**MUSIC, 'ckpt_name': 'other'})
    assert creative_settings.validate_params('dubbing', DUBBING) == DUBBING
    # A VoxCPM2 setting keeps the seed: description plus seed is what recreates the voice.
    assert creative_settings.validate_params('dubbing', {**DUBBING, 'cues': [{'at_seconds': 0}], 'source_asset_id': 'shot',
                                               'idempotency_key': 'x'}) == DUBBING
    # IndexTTS only clones a reference, so its setting freezes without a seed.
    assert creative_settings.validate_params('dubbing', {**DUBBING, 'engine': 'indextts-2.5'}) == {
        key: value for key, value in DUBBING.items() if key != 'seed'} | {'engine': 'indextts-2.5'}
    for bad in ({**DUBBING, 'voice_reference': ''}, {**DUBBING, 'original_audio': 'drop'},
                {**DUBBING, 'original_gain': 4}, {**DUBBING, 'engine': 'auk-flash'},
                {**DUBBING, 'voice_description': '低沉男声'}, {**DUBBING, 'seed': '5'},
                {key: value for key, value in DUBBING.items() if key != 'seed'},
                {**DUBBING, 'emotion': {'mode': 'vector', 'vector': [0.5] * 8}},
                {**DUBBING, 'emotion': {'mode': 'reference', 'reference': 'D:/emo.wav', 'alpha': 0.5}}):
        with pytest.raises(ValueError):
            creative_settings.validate_params('dubbing', bad)
    designed = creative_settings.validate_params('dubbing', {'engine': 'voxcpm-2', 'voice_description': '低沉旁白',
                                                             'seed': 7, 'original_audio': 'mute', 'original_gain': 1,
                                                             'emotion': {'mode': 'text', 'text': '克制'}})
    assert designed['voice_description'] == '低沉旁白' and 'voice_reference' not in designed
    assert designed['emotion']['mode'] == 'text'
    with pytest.raises(ValueError, match='一种音色来源'):
        creative_settings.validate_params('dubbing', {**DUBBING, 'voice_description': '低沉旁白'})
    with pytest.raises(ValueError, match='IndexTTS'):
        creative_settings.validate_params('dubbing', {'engine': 'indextts-2.5', 'voice_description': '低沉旁白',
                                                     'original_audio': 'keep', 'original_gain': 1})
    with pytest.raises(ValueError, match='分区'):
        creative_settings.validate_params('video', {})


def test_save_load_and_remove_roundtrip(tmp_path):
    path = tmp_path / 'creative-settings.json'
    first = creative_settings.save('music', '夜景 88', MUSIC, now=1.0, path=path)
    assert first['params'] == MUSIC and json.loads(path.read_text(encoding='utf-8'))['music'][0]['name'] == '夜景 88'
    creative_settings.save('music', '夜景 88', {**MUSIC, 'bpm': 96}, now=2.0, path=path)
    document = creative_settings.load(path)
    assert len(document['music']) == 1 and document['music'][0]['params']['bpm'] == 96
    assert creative_settings.save('dubbing', '低沉旁白', DUBBING, now=3.0, path=path)['name'] == '低沉旁白'
    assert creative_settings.remove('dubbing', '低沉旁白', path) is True
    assert creative_settings.remove('dubbing', '低沉旁白', path) is False
    assert creative_settings.load(path)['dubbing'] == []


def test_save_refuses_unknown_entries_and_capacity(tmp_path):
    path = tmp_path / 'creative-settings.json'
    for index in range(creative_settings.MAX_SETTINGS):
        creative_settings.save('music', f'设定{index}', MUSIC, now=float(index), path=path)
    with pytest.raises(ValueError, match='最多保存'):
        creative_settings.save('music', '超出的设定', MUSIC, now=99.0, path=path)
    broken = tmp_path / 'broken.json'
    broken.write_text('{', encoding='utf-8')
    with pytest.raises(ValueError, match='无法读取'):
        creative_settings.load(broken)
    broken.write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match='不是 JSON 对象'):
        creative_settings.load(broken)


def test_http_library_saves_loads_and_deletes(preset_client):
    client, _ = preset_client
    assert client.get('/api/creative-settings').json() == {'settings': {'music': [], 'dubbing': []}}
    saved = client.put('/api/creative-settings/music/%E5%A4%9C%E6%99%AF%2088', json={'params': MUSIC})
    assert saved.status_code == 200, saved.text
    assert saved.json()['entry']['name'] == '夜景 88'
    listed = client.get('/api/creative-settings').json()['settings']
    assert [item['name'] for item in listed['music']] == ['夜景 88']
    assert client.put('/api/creative-settings/music/%E5%A4%9C%E6%99%AF%2088',
                      json={'params': {**MUSIC, 'bpm': 96}}).json()['entry']['params']['bpm'] == 96
    assert client.put('/api/creative-settings/dubbing/%E4%BD%8E%E6%B2%89%E6%97%81%E7%99%BD',
                      json={'params': DUBBING}).status_code == 200
    assert client.put('/api/creative-settings/video/x', json={'params': {}}).status_code == 422
    assert client.put('/api/creative-settings/music/%20%20', json={'params': MUSIC}).status_code == 422
    assert client.put('/api/creative-settings/music/x', json={'params': {**MUSIC, 'duration_seconds': 900}}).status_code == 422
    assert client.delete('/api/creative-settings/music/%E6%B2%A1%E6%9C%89%E7%9A%84%E5%90%8D%E5%AD%97').status_code == 404
    assert client.delete('/api/creative-settings/music/%E5%A4%9C%E6%99%AF%2088').json() == {'removed': True}
    assert [item['name'] for item in client.get('/api/creative-settings').json()['settings']['dubbing']] == ['低沉旁白']
