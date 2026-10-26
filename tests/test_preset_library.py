"""预设库：内置起点卡片、用户同名覆盖、删除保护与参数校验。"""
import json

import pytest

from backend import app as backend
from backend import preset_library


TIMBRE = {'engine': 'voxcpm-2', 'voice_description': '四十岁上下的男性，音色低沉干净'}
MUSIC = {'tags': '悬疑氛围，弦乐铺底', 'bpm': 88, 'keyscale': 'A minor', 'timesignature': '4',
         'duration_seconds': 24, 'language': 'unknown'}


def test_builtin_cards_are_read_only_starting_points():
    cards = preset_library.cards('timbre')
    assert all(card['builtin'] and card['modified'] is False for card in cards)
    assert '沉稳中年旁白' in {card['name'] for card in cards}
    music = preset_library.cards('music')
    assert music and all(set(card['params']) <= set(MUSIC) for card in music)


def test_builtin_timbre_cards_only_promise_what_voxcpm_can_do():
    # IndexTTS cannot design a timbre from words, so no built-in may pretend otherwise.
    for card in preset_library.cards('timbre'):
        assert card['params']['engine'] == 'voxcpm-2'
        assert card['params']['voice_description'].strip()


def test_save_replaces_a_builtin_by_name_and_renders_as_modified(tmp_path):
    path = tmp_path / 'preset-library.json'
    preset_library.save('timbre', '沉稳中年旁白', {'engine': 'voxcpm-2', 'voice_description': '五十岁男性，声音沙哑'}, now=1.0, path=path)
    card = next(item for item in preset_library.cards('timbre', path) if item['name'] == '沉稳中年旁白')
    assert card['builtin'] is True and card['modified'] is True
    assert card['params']['voice_description'] == '五十岁男性，声音沙哑'
    assert preset_library.remove('timbre', '沉稳中年旁白', path) is True
    back = next(item for item in preset_library.cards('timbre', path) if item['name'] == '沉稳中年旁白')
    assert back['modified'] is False


def test_own_cards_are_listed_after_builtins(tmp_path):
    path = tmp_path / 'preset-library.json'
    preset_library.save('music', '我的追逐戏', MUSIC, now=2.0, path=path)
    cards = preset_library.cards('music', path)
    mine = next(card for card in cards if card['name'] == '我的追逐戏')
    assert mine['builtin'] is False
    assert cards.index(mine) > max(index for index, card in enumerate(cards) if card['builtin'])


def test_deleting_a_builtin_card_is_refused(tmp_path):
    with pytest.raises(ValueError, match='内置卡片'):
        preset_library.remove('music', '悬疑铺底', tmp_path / 'preset-library.json')


@pytest.mark.parametrize('params', [
    {'engine': 'voxcpm-2'},
    {'engine': 'voxcpm-2', 'voice_description': '低沉男声', 'voice_reference': 'D:/voice.wav'},
    {'engine': 'indextts-2.5', 'voice_description': '低沉男声', 'voice_reference': 'D:/voice.wav'},
    {'engine': 'auk-flash', 'voice_description': '低沉男声'},
    {'engine': 'voxcpm-2', 'voice_description': '低沉男声', 'seed': 5},
])
def test_timbre_cards_reject_shapes_the_engines_cannot_use(params):
    with pytest.raises(ValueError):
        preset_library.validate_params('timbre', params)


def test_timbre_card_can_record_which_engine_made_the_sample():
    frozen = preset_library.validate_params('timbre', {'engine': 'indextts-2.5', 'voice_reference': 'D:/voice.wav',
                                                       'generated_by': 'voxcpm-2'})
    assert frozen['generated_by'] == 'voxcpm-2'
    with pytest.raises(ValueError, match='生成引擎'):
        preset_library.validate_params('timbre', {'voice_description': '低沉男声', 'generated_by': 'auk-flash'})


def test_indextts_card_keeps_only_a_reference_and_audition_line():
    frozen = preset_library.validate_params('timbre', {'engine': 'indextts-2.5', 'voice_reference': 'D:/voice.wav',
                                                       'text': '试一句'})
    assert frozen == {'engine': 'indextts-2.5', 'voice_reference': 'D:/voice.wav', 'text': '试一句'}
    assert preset_library.validate_params('timbre', {'voice_description': '低沉男声'})['engine'] == 'voxcpm-2'


@pytest.mark.parametrize('change', [{'tags': ''}, {'bpm': 9}, {'bpm': '88'}, {'timesignature': '5'},
                                    {'duration_seconds': 900}, {'keyscale': ''}, {'language': 'xx'},
                                    {'lyrics': '[verse]'}])
def test_music_cards_reject_unusable_parameters(change):
    with pytest.raises(ValueError):
        preset_library.validate_params('music', {**MUSIC, **change})


def test_unknown_kind_and_capacity(tmp_path):
    path = tmp_path / 'preset-library.json'
    with pytest.raises(ValueError, match='分区'):
        preset_library.validate_params('video', MUSIC)
    for index in range(preset_library.MAX_CARDS):
        preset_library.save('music', f'卡片{index}', MUSIC, now=float(index), path=path)
    with pytest.raises(ValueError, match='最多保存'):
        preset_library.save('music', '超出的卡片', MUSIC, now=99.0, path=path)
    broken = tmp_path / 'broken.json'
    broken.write_text('{', encoding='utf-8')
    with pytest.raises(ValueError, match='无法读取'):
        preset_library.load(broken)


def test_http_library_lists_saves_and_deletes(preset_client):
    client, _ = preset_client
    listed = client.get('/api/preset-library').json()['library']
    assert set(listed) == {'timbre', 'music'}
    assert listed['timbre'][0]['builtin'] is True
    saved = client.put('/api/preset-library/timbre/%E6%88%91%E7%9A%84%E6%97%81%E7%99%BD',
                       json={'params': TIMBRE})
    assert saved.status_code == 200, saved.text
    assert [item['name'] for item in client.get('/api/preset-library').json()['library']['timbre']
            if item['builtin'] is False] == ['我的旁白']
    assert client.put('/api/preset-library/timbre/x', json={'params': {'engine': 'voxcpm-2'}}).status_code == 422
    assert client.put('/api/preset-library/music/x', json={'params': {**MUSIC, 'bpm': 9}}).status_code == 422
    assert client.put('/api/preset-library/video/x', json={'params': MUSIC}).status_code == 422
    builtin = next(item['name'] for item in preset_library.BUILTIN['music'])
    assert client.delete(f'/api/preset-library/music/{builtin}').status_code == 422
    assert client.delete('/api/preset-library/music/%E6%B2%A1%E6%9C%89%E7%9A%84').status_code == 404
    assert client.delete('/api/preset-library/timbre/%E6%88%91%E7%9A%84%E6%97%81%E7%99%BD').json() == {'removed': True}
    assert json.loads((backend.ROOT / 'config' / 'preset-library.json').read_text(encoding='utf-8'))['timbre'] == []
