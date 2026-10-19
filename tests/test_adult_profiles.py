import json
from pathlib import Path

import pytest

from backend import app as backend
from backend import production_presets, shot_creation
from backend.keyframes import capability, prepare_text
from tools.qwen_image_t2i import graph


def test_adult_video_preset_is_explicit_and_loads_the_safe_recipe():
    preset_id = production_presets.ADULT_REALISM_TEXT_PRESET_ID
    settings = shot_creation.normalize_generation(preset_id, {'creative_profile': 'adult-realism'},
                                                  explicit_fields={'creative_profile'})
    assert settings['creative_profile'] == 'adult-realism'
    recipe = production_presets.load_recipe(preset_id)
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


def test_adult_preset_requires_explicit_adult_profile():
    with pytest.raises(shot_creation.UnsupportedCombination, match='adult-realism'):
        shot_creation.normalize_generation(production_presets.ADULT_REALISM_TEXT_PRESET_ID,
                                           {'creative_profile': 'general'},
                                           explicit_fields={'creative_profile'})


@pytest.mark.parametrize('preset_id', [
    production_presets.TEXT_LANDSCAPE_PRESET_ID,
    production_presets.NATIVE_LANDSCAPE_PRESET_ID,
    production_presets.END_PRESET_IDS[0],
    production_presets.FAST_TEXT_LANDSCAPE_PRESET_ID,
    production_presets.FAST_IMAGE_LANDSCAPE_PRESET_ID,
])
@pytest.mark.parametrize('profile', ['adult-realism', 'adult-anime'])
def test_non_adult_presets_block_adult_profiles_instead_of_falling_back(preset_id, profile):
    with pytest.raises(shot_creation.UnsupportedCombination, match='成人向'):
        shot_creation.normalize_generation(preset_id, {'creative_profile': profile},
                                           explicit_fields={'creative_profile'})


def test_non_adult_presets_load_no_adult_adapter():
    for preset_id in (production_presets.TEXT_LANDSCAPE_PRESET_ID, production_presets.NATIVE_LANDSCAPE_PRESET_ID,
                      production_presets.END_PRESET_IDS[0], production_presets.FAST_TEXT_LANDSCAPE_PRESET_ID,
                      production_presets.FAST_IMAGE_LANDSCAPE_PRESET_ID):
        assert 'adult' not in json.dumps(production_presets.load_recipe(preset_id), ensure_ascii=False).lower()
