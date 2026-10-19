"""Explicit production-plan selection; no model, filesystem, or task execution.

The workbench no longer chooses a recipe.  A shot is saved only after the
caller names a workflow or an already installed production preset (or the
project has one explicitly installed plan).  This module turns that selection
into a workflow and rejects unsupported input combinations instead of falling
back to a different recipe.
"""
from . import production_presets as presets
from mcp_server.creation_inputs import GenerationSettings


class PlanSelectionError(ValueError):
    code = 'unsupported_plan'

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class PlanRequired(PlanSelectionError):
    code = 'creation_plan_required'


class PresetNotInstalled(PlanSelectionError):
    code = 'preset_not_installed'


class UnsupportedCombination(PlanSelectionError):
    code = 'unsupported_combination'


BASE_GENERATION = {
    'aspect_ratio': '16:9',
    'output_resolution': 'native',
    'sound_mode': 'performance_reference',
    'seed': 42,
    'speed_profile': 'standard',
    'creative_profile': 'general',
}
FAST_PRESET_IDS = (presets.FAST_TEXT_LANDSCAPE_PRESET_ID, presets.FAST_IMAGE_LANDSCAPE_PRESET_ID)


def generation_defaults(preset_id: str | None) -> dict:
    if preset_id not in presets.PRESET_IDS:
        return {}
    return {
        'aspect_ratio': presets.aspect_ratio(preset_id),
        'sound_mode': 'locked_dialogue' if preset_id == presets.LOCKED_PRESET_ID else 'performance_reference',
        'speed_profile': 'fast' if preset_id in FAST_PRESET_IDS else 'standard',
        'creative_profile': 'adult-realism' if preset_id in presets.ADULT_PRESET_IDS else 'general',
    }


def _stage_for_preset(manifest: dict, preset_id: str) -> dict | None:
    target = presets.stage_id(preset_id)
    return next((stage for stage in manifest.get('pipeline', [])
                 if isinstance(stage, dict) and stage.get('id') == target), None)


def _workflow_for_stage(stage: dict | None) -> str | None:
    references = ((stage or {}).get('execution') or {}).get('references') or []
    return str(references[0]) if len(references) == 1 and references[0] else None


def stage_id_for_workflow(manifest: dict, workflow: str | None) -> str | None:
    if not workflow:
        return None
    for stage in manifest.get('pipeline', []):
        if not isinstance(stage, dict):
            continue
        if workflow in (((stage.get('execution') or {}).get('references')) or []):
            return str(stage.get('id'))
    return None


def preset_for_workflow(manifest: dict, workflow: str | None) -> str | None:
    if not workflow:
        return None
    return next((preset_id for preset_id in presets.PRESET_IDS
                 if workflow in (((_stage_for_preset(manifest, preset_id) or {}).get('execution') or {}).get('references') or [])), None)


def installed_stages(manifest: dict) -> list[dict]:
    rows = []
    for preset_id in presets.PRESET_IDS:
        stage = _stage_for_preset(manifest, preset_id)
        workflow = _workflow_for_stage(stage)
        if stage is not None and workflow:
            rows.append({'preset_id': preset_id, 'stage_id': presets.stage_id(preset_id),
                         'title': presets.title(preset_id), 'workflow': workflow,
                         'aspect_ratio': presets.aspect_ratio(preset_id)})
    return rows


def creation_options(manifest: dict) -> dict:
    installed = {row['preset_id']: row for row in installed_stages(manifest)}
    return {
        'installed_stages': list(installed.values()),
        'available_presets': [
            {'id': preset_id, 'title': presets.title(preset_id),
             'aspect_ratio': presets.aspect_ratio(preset_id),
             'installed': preset_id in installed,
             'workflow': installed.get(preset_id, {}).get('workflow')}
            for preset_id in presets.PRESET_IDS
        ],
    }


def resolve_plan(manifest: dict, workflow: str | None = None, preset_id: str | None = None) -> tuple[str, str | None]:
    """Return the explicitly selected workflow and its preset (if known).

    Priority is an explicit workflow, then an explicit installed preset, then
    the project's explicitly installed plan.  Missing selection raises
    ``PlanRequired``; an uninstalled preset raises ``PresetNotInstalled``.
    Nothing is installed or written here.
    """
    workflow = workflow.strip() if isinstance(workflow, str) else workflow
    preset_id = preset_id.strip() if isinstance(preset_id, str) else preset_id
    if preset_id:
        if preset_id not in presets.PRESET_IDS:
            raise PlanSelectionError('制作方案不存在', code='unknown_preset')
        stage = _stage_for_preset(manifest, preset_id)
        if stage is None:
            raise PresetNotInstalled(f'制作方案“{presets.title(preset_id)}”尚未安装；请先显式安装后再保存分镜。')
        selected = _workflow_for_stage(stage)
        if not selected:
            raise UnsupportedCombination('已登记的制作方案缺少唯一执行图，不能作为分镜方案。', code='recipe_unresolved')
        return selected, preset_id
    if workflow:
        return workflow, preset_for_workflow(manifest, workflow)
    selected = manifest.get('production_preset') or {}
    selected_id = selected.get('id')
    if selected_id in presets.PRESET_IDS:
        selected_workflow = _workflow_for_stage(_stage_for_preset(manifest, selected_id)) or selected.get('workflow')
        if selected_workflow:
            return str(selected_workflow), selected_id
    raise PlanRequired('请显式选择制作方案或工作流；工作台不会代为选择。')


def normalize_generation(preset_id: str | None, provided: dict | None = None, *, explicit_fields: set[str] | None = None,
                         first: str | None = None, last: str | None = None, guide: str | None = None) -> dict:
    """Merge parameters with the selected plan's fixed facts and validate them.

    Only combinations that would silently change or ignore the caller's chosen
    plan are rejected here.  Missing required materials remain a preflight
    blocker, as before.
    """
    explicit = set(explicit_fields or ())
    settings = dict(BASE_GENERATION)
    settings.update(generation_defaults(preset_id))
    if provided:
        settings.update(provided)
    if preset_id not in presets.PRESET_IDS:
        if settings.get('creative_profile') in {'adult-realism', 'adult-anime'}:
            raise UnsupportedCombination(
                '自定义工作流未声明成人向视频配方，不能按成人向生成；请选择已登记的成人向制作方案。',
                code='unsupported_creative_profile')
        return settings

    if preset_id in presets.TEXT_PRESET_IDS:
        if first or last or guide:
            raise UnsupportedCombination('纯文本制作方案不接受首尾帧或表演声音；请清除这些素材或选择对应方案。',
                                         code='unsupported_creation_inputs')
    elif preset_id in presets.END_PRESET_IDS:
        if guide:
            raise UnsupportedCombination('首尾帧制作方案不接受表演声音；请清除声音引导或选择首帧方案。',
                                         code='unsupported_creation_inputs')
    elif preset_id in (presets.LOCKED_PRESET_ID, presets.FIRST_PRESET_ID):
        if last:
            raise UnsupportedCombination('首帧制作方案不接受结束画面；请清除结束画面或选择首尾帧方案。',
                                         code='unsupported_creation_inputs')
    elif preset_id != presets.PRESET_ID:
        if last or guide:
            raise UnsupportedCombination('首帧原生声音制作方案不接受结束画面或表演声音；请清除对应素材。',
                                         code='unsupported_creation_inputs')

    aspect = presets.aspect_ratio(preset_id)
    if 'aspect_ratio' in explicit and settings.get('aspect_ratio') != aspect:
        raise UnsupportedCombination(
            f'所选制作方案固定为 {aspect}，与请求的 {settings.get("aspect_ratio")} 不一致；请改选对应画幅方案。',
            code='preset_aspect_mismatch')
    settings['aspect_ratio'] = aspect

    sound = 'locked_dialogue' if preset_id == presets.LOCKED_PRESET_ID else 'performance_reference'
    if 'sound_mode' in explicit and settings.get('sound_mode') != sound:
        raise UnsupportedCombination(
            '所选制作方案固定使用锁定对白声音' if sound == 'locked_dialogue' else '所选制作方案使用原生声音或表演参考，不接受锁定对白',
            code='preset_sound_mode_mismatch')
    settings['sound_mode'] = sound

    speed = 'fast' if preset_id in FAST_PRESET_IDS else 'standard'
    if 'speed_profile' in explicit and settings.get('speed_profile') != speed:
        raise UnsupportedCombination(
            '所选制作方案固定为快速预览' if speed == 'fast' else '所选制作方案固定为标准采样，不接受快速预览',
            code='preset_speed_profile_mismatch')
    settings['speed_profile'] = speed

    adult_preset = preset_id in presets.ADULT_PRESET_IDS
    profile = settings.get('creative_profile', 'general')
    if profile == 'adult-anime' and not adult_preset:
        raise UnsupportedCombination(
            '视频生成目前没有对应的成人向动漫配方（动漫质感只在关键帧图像加载 Qwen Anime Consistency）。',
            code='unsupported_creative_profile')
    if profile == 'adult-realism' and not adult_preset:
        raise UnsupportedCombination(
            '成人向真人质感视频配方目前只对应已登记的成人向制作方案；请显式选择该方案。',
            code='unsupported_creative_profile')
    if adult_preset and profile != 'adult-realism':
        raise UnsupportedCombination('成人向制作方案必须显式使用 adult-realism 创作分区。',
                                     code='unsupported_creative_profile')
    settings['creative_profile'] = 'adult-realism' if adult_preset else 'general'
    return settings
