"""Deterministic high-level selection; no model, filesystem, or task execution."""
from . import production_presets as presets
from mcp_server.creation_inputs import GenerationSettings


def resolve(settings, *, first=None, last=None, guide=None):
    aspect = settings['aspect_ratio']
    sound = settings['sound_mode']
    if last and not first:
        raise ValueError('结束画面目前需要同时选择开始画面；请补开始画面或清除结束画面。')
    if sound == 'locked_dialogue' and not guide:
        raise ValueError('严格使用已有对白需要明确选择表演声音。')
    if guide:
        if not first or aspect != '9:16':
            raise ValueError('当前表演声音控制仅支持竖屏并有开始画面；请选择竖屏和开始画面，或明确清除声音控制。')
        if sound == 'locked_dialogue':
            if last:
                raise ValueError('当前严格对白不支持结束画面；请选择表演参考或明确清除结束画面。')
            recipe = presets.LOCKED_PRESET_ID
        else:
            recipe = presets.PRESET_ID if last else presets.FIRST_PRESET_ID
    else:
        shapes = ('16:9', '1:1', '9:16')
        if not first:
            recipe = presets.TEXT_PRESET_IDS[shapes.index(aspect)]
        elif last:
            recipe = presets.END_PRESET_IDS[shapes.index(aspect)]
        else:
            recipe = presets.NATIVE_LANDSCAPE_PRESET_ID if aspect == '16:9' else presets.NATIVE_SQUARE_PRESET_ID if aspect == '1:1' else presets.NATIVE_PORTRAIT_PRESET_ID
    return {'preset_id': recipe, 'generation_mode': 'image_to_video' if first else 'text_to_video',
            'aspect_ratio': aspect, 'megapixels': 0.4,
            'sound_mode': sound if guide else 'native',
            'selection_basis': {'first_frame_bound': bool(first), 'last_frame_bound': bool(last), 'performance_audio_bound': bool(guide)}}
