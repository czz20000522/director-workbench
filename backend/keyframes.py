"""Freeze image-edit inputs for the workbench's shared task runner."""
from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

import av

from tools.qwen_image_edit import graph
from tools.qwen_image_t2i import graph as text_graph
from . import inpaint_recipe


def _image_loras(profile: str | None) -> list[str]:
    """创作分区 → LoRA 链。

    前两条是角色/画风锚（把任意参考拉成真人或拉成同一套动漫脸），
    第三条 `qwen-image-2.1-uncensored-lora` 才是真正去掉内容限制的那一层，
    所以它只跟在成人向分区后面，常规分区一条都不挂。
    """
    base = {
        'adult-realism': r'adult-safe\Qwen2.1_Anything2RealCharacters.safetensors',
        'adult-anime': r'adult-safe\Qwen2.1_Anime_consistency.safetensors',
    }.get(profile or 'general')
    if not base:
        return []
    return [base, 'qwen-image-2.1-uncensored-lora.safetensors']


def _image_lora(profile: str | None) -> str | None:
    chain = _image_loras(profile)
    return chain[0] if chain else None


REFERENCE_ROLES = ('底图', '脸型', '造型', '服装', '姿势')

# 每个槽在提示词里的说法。'底图' 是"以这张为准"的意思，用于改图时锁住整体，
# 不能写成"参考"，否则模型会把它当成又一张素材去平均。
_ROLE_TEXT = {
    '底图': '参考图{index}是当前画面，以它为准做修改',
    '脸型': '参考图{index}是人物五官',
    '造型': '参考图{index}是发型与整体造型',
    '服装': '参考图{index}是一套服装',
    '姿势': '参考图{index}是姿势参考',
    # 兼容 11-07 之前存的旧任务：那时只有一个笼统的「人像」槽。
    '人像': '参考图{index}是人物五官',
}


def build_reference_prefix(references: list[dict]) -> str:
    """Name every reference slot in the prompt.

    实测：只说“穿上参考图中的这套服装”，模型会自己编一套完全不同的衣服；
    点名“参考图2 是一件酒红色蝴蝶结衬衫”就照做了。所以角色说明不能省。
    """
    parts: list[str] = []
    for index, item in enumerate(references, 1):
        role = str(item.get('role') or '').strip()
        label = str(item.get('label') or '').strip()
        text = _ROLE_TEXT.get(role, '参考图{index}是补充视觉参考').format(index=index)
        if label:
            text = f'{text}（{label}）'
        parts.append(text)
    return '；'.join(parts)


def prepare_text(task_id: str, prompt: str, snapshot_root: Path, output_prefix: str,
                 seed: int, width: int = 768, height: int = 1344,
                 creative_profile: str = 'general', steps: int = 8,
                 references: list[dict] | None = None,
                 refine: bool = False) -> dict:
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('关键帧任务 ID 无效')
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen: list[Path] = []
    slots: list[dict] = []
    for index, item in enumerate(references or [], 1):
        source = Path(str(item.get('path') or '')).resolve()
        if not source.is_file() or source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
            raise ValueError(f'参考图不可读取：{item.get("role") or index}')
        target = directory / f'reference-{index}{source.suffix.lower()}'
        shutil.copy2(source, target)
        frozen.append(target.resolve())
        slots.append({'role': str(item.get('role') or ''), 'label': str(item.get('label') or ''),
                      'path': str(target.resolve())})
    prefix_text = build_reference_prefix(slots)
    graph_prompt = f'{prefix_text}。{prompt}' if prefix_text else prompt
    recipe = text_graph(graph_prompt, output_prefix, seed, width, height,
                        None, steps, frozen or None, _image_loras(creative_profile),
                        refine=refine)
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'mode': 'reference' if slots else 'text',
               'engine': 'Qwen Image 2.1 · 多参考' if slots else 'Qwen Image 2.1',
               'creative_profile': creative_profile, 'prompt': prompt, 'prompt_for_model': graph_prompt,
               'reference_prefix': prefix_text, 'references': slots,
               'seed': seed, 'output_prefix': output_prefix, 'width': width, 'height': height,
               'steps': steps, 'refine': refine}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'keyframe', 'workflow': str(workflow.resolve()), 'request': request}



def prepare_inpaint(task_id: str, prompt: str, source: Path, mask_bytes: bytes,
                    snapshot_root: Path, output_prefix: str, seed: int,
                    steps: int = 8, denoise: float = 0.75,
                    creative_profile: str = 'general') -> dict:
    """Freeze a masked edit: the source frame, the painted mask and the graph.

    `mask_bytes` is a PNG exported by the browser canvas (white = repaint).
    Both images are copied into the task snapshot so a later run cannot drift.
    """
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('关键帧任务 ID 无效')
    if not mask_bytes or len(mask_bytes) > 12 * 1024 * 1024:
        raise ValueError('蒙版为空或超过 12 MB')
    if not mask_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('蒙版必须是 PNG')
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise ValueError('重绘底图不可读取')
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen_source = directory / ('source' + source.suffix.lower())
    shutil.copy2(source, frozen_source)
    frozen_mask = directory / 'mask.png'
    frozen_mask.write_bytes(mask_bytes)
    try:
        with av.open(str(frozen_source)) as media:
            frame = next(media.decode(video=0))
            width, height = frame.width, frame.height
        with av.open(str(frozen_mask)) as media:
            mask_frame = next(media.decode(video=0))
            mask_size = (mask_frame.width, mask_frame.height)
    except (av.FFmpegError, StopIteration, ValueError, IndexError) as exc:
        raise ValueError('重绘底图或蒙版无法解码') from exc
    if mask_size != (width, height):
        raise ValueError(f'蒙版尺寸 {mask_size[0]}×{mask_size[1]} 与底图 {width}×{height} 不一致')
    # 以「当前画面为准」的说明放在最前，模型才知道原图不是又一张风格素材。
    graph_prompt = f'参考图1是当前画面，只重绘蒙版内区域，蒙版外保持原样。{prompt}'
    recipe = inpaint_recipe.graph(graph_prompt, output_prefix, seed, frozen_source, frozen_mask,
                                  width, height, steps, denoise, _image_loras(creative_profile))
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'mode': 'inpaint', 'engine': 'Qwen Image 2.1 · 局部重绘',
               'creative_profile': creative_profile, 'prompt': prompt, 'prompt_for_model': graph_prompt,
               'reference_image': str(frozen_source.resolve()), 'mask': str(frozen_mask.resolve()),
               'seed': seed, 'steps': steps, 'denoise': float(denoise),
               'output_prefix': output_prefix, 'width': width, 'height': height}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'keyframe', 'workflow': str(workflow.resolve()), 'request': request}


def capability(object_info: dict, mode: str = 'reference', creative_profile: str = 'general') -> dict:
    """Use the running ComfyUI registry, including its actual model choices."""
    if creative_profile not in {'general', 'adult-realism', 'adult-anime'}:
        raise ValueError('创作分区无效')
    image_lora = _image_lora(creative_profile)
    # 参考图模式自 multi-reference 改造起走同一套 Qwen Image 2.1 权重：
    # 参考图由 TextEncodeQwenImage21 的 images 编成 reference latents，
    # 不再依赖未安装的 Edit 2511，所以两种模式的能力面完全一致。
    if mode == 'inpaint':
        recipe = inpaint_recipe.graph('能力检查', 'keyframe', 0, Path('probe.png'), Path('probe-mask.png'),
                                      768, 1344, lora_names=_image_loras(creative_profile), check_files=False)
    else:
        recipe = text_graph('能力检查', 'keyframe', 0, lora_name=image_lora)
    missing_nodes, missing_models = set(), set()
    for node in recipe.values():
        name = node['class_type']
        definition = object_info.get(name)
        if not isinstance(definition, dict):
            missing_nodes.add(name)
            continue
        required = definition.get('input', {}).get('required', {})
        for field in ('clip_name', 'unet_name', 'vae_name', 'lora_name'):
            if field not in node['inputs']:
                continue
            specification = required.get(field, [])
            choices = specification[0] if specification and isinstance(specification[0], list) else []
            if node['inputs'][field] not in choices:
                missing_models.add(node['inputs'][field])
    return {'available': not missing_nodes and not missing_models,
            'engine': ('Qwen Image 2.1' if mode == 'text' else
                       'Qwen Image 2.1 · 局部重绘' if mode == 'inpaint' else 'Qwen Image 2.1 · 多参考'),
            'requires_reference_image': mode != 'text', 'creative_profile': creative_profile,
            'missing_nodes': sorted(missing_nodes), 'missing_models': sorted(missing_models),
            'note': '节点与模型登记就绪不代表显存充足或画面质量已确认'}


def read_result(history: dict, prompt_id: str, payload: dict, output_root: Path) -> dict | None:
    entry = history.get(prompt_id)
    if not entry or entry.get('status', {}).get('status_str') not in {'success', 'error'}:
        return None
    if entry['status']['status_str'] == 'error':
        return {'status': 'error', 'raw_status': entry['status']}
    images = entry.get('outputs', {}).get('18', {}).get('images', [])
    if len(images) != 1 or images[0].get('type') != 'output':
        raise ValueError('关键帧成功回执没有唯一的保存图片')
    item = images[0]
    output = (output_root / str(item.get('subfolder', '')) / str(item.get('filename', ''))).resolve()
    expected = (output_root / payload['request']['output_prefix']).resolve()
    if (not output.is_relative_to(output_root.resolve()) or output.parent != expected.parent
            or not output.name.startswith(expected.name + '_') or output.suffix.lower() != '.png'):
        raise ValueError('关键帧输出与本次任务不匹配')
    with av.open(str(output)) as media:
        frame = next(media.decode(video=0))
    return {'status': 'success', 'output': str(output), 'width': frame.width, 'height': frame.height}


def prepare(task_id: str, prompt: str, reference: Path, snapshot_root: Path,
            output_prefix: str, seed: int, creative_profile: str = 'general') -> dict:
    """Build an isolated recipe, without submitting or changing a shot's selection.

    The API caller must resolve reference and snapshot_root within the project's
    allowed directories before calling this function.
    """
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('关键帧任务 ID 无效')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('随机编号必须是有效的非负整数')
    prefix = PurePosixPath(output_prefix)
    if (not output_prefix or prefix.is_absolute() or '\\' in output_prefix
            or ':' in output_prefix or any(part in {'', '.', '..'} for part in output_prefix.split('/'))):
        raise ValueError('输出前缀必须是输出目录内的相对路径')
    reference = reference.resolve()
    if not reference.is_file() or reference.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise ValueError('参考图片不可读取')
    # Verify actual media before reserving a task directory; an extension alone
    # must not admit a corrupt upload to an expensive GPU job.
    try:
        with av.open(str(reference)) as media:
            frame = next(media.decode(video=0))
            width, height = frame.width, frame.height
    except (av.FFmpegError, StopIteration, ValueError, IndexError) as exc:
        raise ValueError('参考图片无法解码') from exc
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    frozen = directory / ('reference' + reference.suffix.lower())
    shutil.copy2(reference, frozen)
    # The adult image adapters were trained for Qwen Image 2.1 T2I; keep
    # reference-edit mode on its native graph until a matching edit adapter is
    # installed and verified.
    recipe = graph(frozen.resolve(), prompt, output_prefix, seed, (width, height))
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'creative_profile': creative_profile, 'prompt': prompt, 'reference_image': str(frozen.resolve()),
               'seed': seed, 'output_prefix': output_prefix,
               'reference_width': width, 'reference_height': height,
               'width': recipe['16']['inputs']['width'], 'height': recipe['16']['inputs']['height']}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'keyframe', 'workflow': str(workflow.resolve()), 'request': request}
