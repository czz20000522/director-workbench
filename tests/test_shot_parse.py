"""分镜提议：只拆分不改写，字段没写就留空。"""
import pytest

from backend import shot_parse


def test_shots_keep_original_wording_and_drop_empty_or_invalid_rows():
    shots = shot_parse.shots_from_payload({'shots': [
        {'prompt': '雨夜，林夏站在便利店门口，手里攥着保温杯', 'duration_seconds': 4,
         'shot_size': '中景', 'camera': '固定机位', 'location': '便利店门口',
         'performance': '她把杯子握紧', 'wardrobe': '米色风衣'},
        {'prompt': '   ', 'duration_seconds': 4},
        {'prompt': '切到出租屋，台灯昏黄', 'duration_seconds': 99},
        'not-a-dict',
    ]})
    assert len(shots) == 2
    assert shots[0]['prompt'].startswith('雨夜，林夏')
    assert shots[0]['shot_size'] == '中景'
    # 越界时长归 0，交给界面用默认时长，而不是照抄一个不可能的值。
    assert shots[1]['duration_seconds'] == 0
    assert shots[1]['shot_size'] == ''


def test_shots_cap_at_twelve_and_require_json():
    shots = shot_parse.shots_from_payload({'shots': [
        {'prompt': f'镜头 {index}', 'duration_seconds': 4} for index in range(20)]})
    assert len(shots) == shot_parse.MAX_SHOTS == 12
    with pytest.raises(ValueError):
        shot_parse.shots_from_payload({'items': []})
    with pytest.raises(ValueError):
        shot_parse.extract_json('模型只说了句话')


def test_extract_json_survives_code_fences():
    parsed = shot_parse.extract_json('```json\n{"shots":[{"prompt":"她在窗边回头"}]}\n```')
    assert parsed['shots'][0]['prompt'] == '她在窗边回头'
