"""Freeze image-edit inputs for the workbench's shared task runner."""
from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

import av

from tools.qwen_image_edit import graph
from tools.krea_text_image import graph as text_graph


def prepare_text(task_id: str, prompt: str, snapshot_root: Path, output_prefix: str,
                 seed: int, width: int = 768, height: int = 1344) -> dict:
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('关键帧任务 ID 无效')
    recipe = text_graph(prompt, output_prefix, seed, width, height)
    directory = snapshot_root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'mode': 'text', 'engine': 'Krea 2 Turbo', 'prompt': prompt,
               'seed': seed, 'output_prefix': output_prefix, 'width': width, 'height': height}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'keyframe', 'workflow': str(workflow.resolve()), 'request': request}


def capability(object_info: dict, mode: str = 'reference') -> dict:
    """Use the running ComfyUI registry, including its actual model choices."""
    recipe = text_graph('能力检查', 'keyframe', 0) if mode == 'text' else graph(Path('reference.png'), '', 'keyframe', 0)
    missing_nodes, missing_models = set(), set()
    for node in recipe.values():
        name = node['class_type']
        definition = object_info.get(name)
        if not isinstance(definition, dict):
            missing_nodes.add(name)
            continue
        required = definition.get('input', {}).get('required', {})
        for field in ('clip_name', 'unet_name', 'vae_name'):
            if field not in node['inputs']:
                continue
            specification = required.get(field, [])
            choices = specification[0] if specification and isinstance(specification[0], list) else []
            if node['inputs'][field] not in choices:
                missing_models.add(node['inputs'][field])
    return {'available': not missing_nodes and not missing_models, 'engine': 'Krea 2 Turbo' if mode == 'text' else 'Qwen Image Edit 2511',
            'requires_reference_image': mode != 'text', 'missing_nodes': sorted(missing_nodes),
            'missing_models': sorted(missing_models), 'note': '节点与模型登记就绪不代表显存充足或画面质量已确认'}


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
            output_prefix: str, seed: int) -> dict:
    """Build an isolated recipe, without submitting or changing a shot's selection.

    The API caller must resolve reference and snapshot_root within the project's
    allowed directories before calling this function.
    """
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('关键帧任务 ID 无效')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError('画面描述长度须为 1 至 6000 字符')
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError('种子必须是有效的非负整数')
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
    recipe = graph(frozen.resolve(), prompt, output_prefix, seed, (width, height))
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'prompt': prompt, 'reference_image': str(frozen.resolve()),
               'seed': seed, 'output_prefix': output_prefix,
               'reference_width': width, 'reference_height': height,
               'width': recipe['16']['inputs']['width'], 'height': recipe['16']['inputs']['height']}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'keyframe', 'workflow': str(workflow.resolve()), 'request': request}
