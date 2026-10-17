import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import production_presets, shot_creation
from backend.keyframes import capability, prepare_text
from tools.qwen_image_t2i import graph


def test_adult_video_profiles_select_safe_recipes():
    selected = shot_creation.resolve({
        'aspect_ratio': '16:9', 'sound_mode': 'performance_reference',
        'speed_profile': 'standard', 'creative_profile': 'adult-realism',
    })
    assert selected['preset_id'] == production_presets.ADULT_REALISM_TEXT_PRESET_ID
    recipe = production_presets.load_recipe(selected['preset_id'])
    assert recipe['105:121']['inputs']['lora_name'] == r'adult-safe\h3-realism-people-t2v-i2v-r2v.safetensors'
    assert recipe['105:126']['inputs']['value'] is False


def test_adult_image_graph_records_qwen_lora(tmp_path):
    recipe = graph('虚构成年人的成熟时尚肖像，服装完整，非露骨', 'project/image', 42,
                   768, 1344, r'adult-safe\Qwen2.1_Anything2RealCharacters.safetensors')
    assert recipe['5']['class_type'] == 'LoraLoaderModelOnly'
    assert recipe['5']['inputs']['lora_name'] == r'adult-safe\Qwen2.1_Anything2RealCharacters.safetensors'
    payload = prepare_text('adult-image', '虚构成年人的成熟时尚肖像，服装完整，非露骨', tmp_path,
                           'project/image', 42, 768, 1344, 'adult-realism')
    saved = json.loads(Path(payload['workflow']).read_text(encoding='utf-8'))
    assert saved['5']['inputs']['lora_name'] == r'adult-safe\Qwen2.1_Anything2RealCharacters.safetensors'
    assert payload['request']['creative_profile'] == 'adult-realism'


def test_adult_image_capability_includes_lora_requirement():
    object_info = {
        'UNETLoader': {'input': {'required': {'unet_name': [['qwen_image_2.1_int8_convrot.safetensors']]}}},
        'CLIPLoader': {'input': {'required': {'clip_name': [['qwen3vl_8b_int8_convrot.safetensors']]}}},
        'VAELoader': {'input': {'required': {'vae_name': [['qwen_image_2.1_vae_bf16.safetensors']]}}},
        'TextEncodeQwenImage21': {'input': {'required': {}}},
        'LoraLoaderModelOnly': {'input': {'required': {'lora_name': [[r'adult-safe\Qwen2.1_Anything2RealCharacters.safetensors']]}}},
        'QwenImage21Cache': {'input': {'required': {}}},
        'EmptyLatentImage': {'input': {'required': {}}},
        'KSampler': {'input': {'required': {}}},
        'VAEDecode': {'input': {'required': {}}},
        'SaveImage': {'input': {'required': {}}},
    }
    result = capability(object_info, 'text', 'adult-realism')
    assert result['available']
    assert result['creative_profile'] == 'adult-realism'


@pytest.mark.parametrize('prompt', ['未成年人肖像', 'celebrity face swap', 'explicit sex scene', '成熟女性裸体肖像', 'genital close-up', '脱去衣物'])
def test_adult_prompt_boundary_rejects_prohibited_requests(prompt):
    with pytest.raises(backend.HTTPException, match='仅支持虚构成年人和非露骨内容'):
        backend.validate_creative_prompt(prompt, 'adult-realism')


APPLIED_COMBINATIONS = [
    ('general', '16:9', 'standard', True),
    ('general', '9:16', 'standard', True),
    ('adult-realism', '16:9', 'standard', True),
    ('adult-realism', '16:9', 'fast', False),
    ('adult-realism', '1:1', 'standard', False),
    ('adult-realism', '9:16', 'standard', False),
    ('adult-anime', '16:9', 'standard', False),
    ('adult-anime', '16:9', 'fast', False),
    ('adult-anime', '1:1', 'standard', False),
    ('adult-anime', '9:16', 'standard', False),
]


@pytest.mark.parametrize('creative,aspect,speed,applied', APPLIED_COMBINATIONS)
def test_video_reports_whether_requested_profile_was_applied(creative, aspect, speed, applied):
    selected = shot_creation.resolve({'aspect_ratio': aspect, 'sound_mode': 'native',
                                      'speed_profile': speed, 'creative_profile': creative})
    assert selected['creative_profile'] == creative
    assert selected['creative_profile_applied'] is applied
    if applied:
        assert selected['creative_profile_note'] == ''
    else:
        assert selected['creative_profile_note']
        assert selected['preset_id'] not in production_presets.ADULT_PRESET_IDS


def test_adult_anime_video_never_promises_an_adult_recipe():
    for aspect in ('16:9', '1:1', '9:16'):
        for first, last in ((None, None), ('first', None), ('first', 'last')):
            shared = {'aspect_ratio': aspect, 'sound_mode': 'native', 'speed_profile': 'standard'}
            anime = shot_creation.resolve({**shared, 'creative_profile': 'adult-anime'}, first=first, last=last)
            plain = shot_creation.resolve({**shared, 'creative_profile': 'general'}, first=first, last=last)
            assert anime['preset_id'] == plain['preset_id']
            assert anime['creative_profile_applied'] is False
            assert 'Qwen Anime Consistency' in anime['creative_profile_note']


def test_fallback_recipes_load_no_adult_adapter():
    for preset_id in (production_presets.TEXT_LANDSCAPE_PRESET_ID, production_presets.NATIVE_LANDSCAPE_PRESET_ID,
                      production_presets.END_PRESET_IDS[0], production_presets.FAST_TEXT_LANDSCAPE_PRESET_ID,
                      production_presets.FAST_IMAGE_LANDSCAPE_PRESET_ID):
        assert 'adult' not in json.dumps(production_presets.load_recipe(preset_id), ensure_ascii=False).lower()
