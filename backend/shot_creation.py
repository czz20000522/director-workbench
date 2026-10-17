"""Deterministic high-level selection; no model, filesystem, or task execution."""
from . import production_presets as presets
from mcp_server.creation_inputs import GenerationSettings

# Both adult LoRAs (Qwen Anything2Real Characters and Qwen Anime Consistency)
# are loaded on the keyframe image path only, see backend/keyframes.py. The
# video path has a single adult recipe family and it covers just one
# combination, so the remaining requests must be reported instead of silently
# echoing the requested profile back as if it had been applied.
LIMITS = {
    'adult-anime': '视频生成目前没有对应的成人向动漫配方（动漫质感只在关键帧图像加载 Qwen Anime Consistency）。',
    'adult-realism': '成人向真人质感视频配方目前只在 16:9、标准步数且未绑定表演声音时可用。',
}


def fallback_note(creative: str, recipe: str) -> str:
    """Name the recipe that was actually selected.

    The fallback can be a fast preview, a performance-audio recipe or another
    canvas size, so the note must not claim a native recipe when the selected
    one is something else."""
    return f'{LIMITS[creative]}本次改用「{presets.title(recipe)}」，其中不含成人向配方。'


def resolve(settings, *, first=None, last=None, guide=None):
    aspect = settings['aspect_ratio']
    sound = settings['sound_mode']
    speed = settings.get('speed_profile', 'standard')
    creative = settings.get('creative_profile', 'general')
    if creative not in {'general', 'adult-realism', 'adult-anime'}:
        raise ValueError('创作分区无效')
    if last and not first:
        raise ValueError('结束画面目前需要同时选择开始画面；请补开始画面或清除结束画面。')
    if sound == 'locked_dialogue' and not guide:
        raise ValueError('严格使用已有对白需要明确选择表演声音。')
    if speed == 'fast' and (aspect != '16:9' or last or guide):
        raise ValueError('快速预览方案当前仅支持 16:9、单段首帧或纯文本输入；请切回标准方案，或清除结束画面和表演声音。')
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
        if creative == 'adult-realism' and aspect == '16:9' and speed != 'fast' and not guide:
            recipe = presets.ADULT_REALISM_TEXT_PRESET_ID if not first else presets.ADULT_REALISM_IMAGE_PRESET_ID
        elif speed == 'fast' and not first:
            recipe = presets.FAST_TEXT_LANDSCAPE_PRESET_ID
        elif speed == 'fast' and first:
            recipe = presets.FAST_IMAGE_LANDSCAPE_PRESET_ID
        elif not first:
            recipe = presets.TEXT_PRESET_IDS[shapes.index(aspect)]
        elif last:
            recipe = presets.END_PRESET_IDS[shapes.index(aspect)]
        else:
            recipe = presets.NATIVE_LANDSCAPE_PRESET_ID if aspect == '16:9' else presets.NATIVE_SQUARE_PRESET_ID if aspect == '1:1' else presets.NATIVE_PORTRAIT_PRESET_ID
    if creative == 'general' or recipe in presets.ADULT_PRESET_IDS:
        applied, note = True, ''
    else:
        applied, note = False, fallback_note(creative, recipe)
    return {'preset_id': recipe, 'generation_mode': 'image_to_video' if first else 'text_to_video',
            'aspect_ratio': aspect, 'megapixels': presets.megapixels(recipe),
            'turbo_sampling': 'enable' if presets.turbo_default(recipe) else 'disable',
            'speed_profile': speed,
            'creative_profile': creative,
            'creative_profile_applied': applied,
            'creative_profile_note': note,
            'sound_mode': sound if guide else 'native',
            'selection_basis': {'first_frame_bound': bool(first), 'last_frame_bound': bool(last), 'performance_audio_bound': bool(guide)}}
