"""Freeze image-edit inputs for the workbench's shared task runner."""
from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

import av
from PIL import Image, ImageDraw, ImageFilter

from tools import qwen_union_control
from tools.qwen_image_edit import graph
from tools.qwen_image_t2i import CONTROL_STRENGTH, graph as text_graph
from . import h3_duration
from . import inpaint_recipe
from . import style_blocks


def _crop_to_aspect(source: Path, width: int, height: int, target: Path) -> Path:
    """把参考图按目标比例裁切，保持原始像素尺寸（不放大）。

    Union 的参考编码会用第一张参考图决定输出尺寸；这里只统一比例，
    让 ReferenceEncode 自己按 resolution 缩放。提前放大到 1344×768 会让人脸
    占满参考画面，模型容易把背景也画成一条竖幅面板。
    """
    try:
        with Image.open(source) as image:
            image = image.convert('RGB')
            # 视频首帧统一用 16:9 / 9:16 的参考比例；用 1344/768=1.75 这类
            # 近似的画布比例会让参考人像的裁切位置和训练分布对不上。
            ratio = 16 / 9 if width >= height else 9 / 16
            current = image.width / image.height
            if current > ratio:
                crop_width = max(1, round(image.height * ratio))
                left = max(0, (image.width - crop_width) // 2)
                image.crop((left, 0, left + crop_width, image.height)).save(target)
            else:
                crop_height = max(1, round(image.width / ratio))
                # 人脸参考取「额头到肩」这一段，比正中裁到鼻子更接近常用人像参考。
                top = max(0, min(image.height - crop_height, round(image.height * 0.08)))
                image.crop((0, top, image.width, top + crop_height)).save(target)
    except (OSError, ValueError) as exc:
        raise ValueError(f'参考图无法裁切：{source.name}') from exc
    return target


def _fit_control_image(source: Path, width: int, height: int, target: Path,
                       background_source: Path | None = None) -> Path:
    """把姿势控制图按比例放进目标画幅，边缘做羽化过渡。

    姿势库常见 3:4（900×1200），成片可能是 16:9。直接拉伸会把人体压扁；
    硬补边又会在控制图里留下一条竖线，模型会照着画成白墙/侧栏。
    这里保持比例居中，并把贴合边缘的 alpha 渐变到背景色，消除硬边。

    ``background_source`` 给了参考图时，用参考图角落的墙面纹理做底，
    比纯色更接近真实背景，模型不会把补边区画成一块白色板子。
    """
    try:
        with Image.open(source) as image:
            image = image.convert('RGB')
            scale = min(width / image.width, height / image.height)
            fitted = image.resize((max(1, round(image.width * scale)),
                                   max(1, round(image.height * scale))), Image.LANCZOS)
            corners = [fitted.getpixel((0, 0)), fitted.getpixel((fitted.width - 1, 0)),
                       fitted.getpixel((0, fitted.height - 1)),
                       fitted.getpixel((fitted.width - 1, fitted.height - 1))]
            background = tuple(sum(pixel[channel] for pixel in corners) // 4 for channel in range(3))
            if background_source is not None:
                with Image.open(background_source) as wall:
                    wall = wall.convert('RGB')
                    patch = wall.crop((0, 0, min(80, wall.width), min(80, wall.height)))
                    canvas = patch.resize((width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(6))
            else:
                canvas = Image.new('RGB', (width, height), background)
            mask = Image.new('L', fitted.size, 255)
            draw = ImageDraw.Draw(mask)
            feather = max(1, min(96, fitted.width // 8, fitted.height // 8))
            pad_x = width - fitted.width
            pad_y = height - fitted.height
            for step in range(feather):
                alpha = int(255 * step / feather)
                if pad_x > 0:
                    draw.line([(step, 0), (step, fitted.height - 1)], fill=alpha)
                    draw.line([(fitted.width - 1 - step, 0), (fitted.width - 1 - step, fitted.height - 1)], fill=alpha)
                if pad_y > 0:
                    draw.line([(0, step), (fitted.width - 1, step)], fill=alpha)
                    draw.line([(0, fitted.height - 1 - step), (fitted.width - 1, fitted.height - 1 - step)], fill=alpha)
            canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2), mask)
            canvas.save(target)
    except (OSError, ValueError) as exc:
        raise ValueError(f'姿势控制图无法读取：{source.name}') from exc
    return target


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
                 refine: bool = False, style_block: str | None = None) -> dict:
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
                      'category': str(item.get('category') or ''),
                      'control': str(item.get('control') or 'auto'),
                      'path': str(target.resolve())})
    prefix_text = build_reference_prefix([slot for slot in slots if slot['role'] != '姿势'])
    semantic = [slot for slot in slots if slot['role'] != '姿势']
    control_slot = next((slot for slot in slots if slot['role'] == '姿势'), None)
    control_path = None
    control_mode = None
    if control_slot is not None:
        # 姿势槽走 ControlNet 的结构控制图；按目标画幅补边，避免骨架被拉伸。
        # 线稿也能用，但实测线稿上的关节圆圈会被画进衣服，深度图更干净。
        control_path = _fit_control_image(
            Path(control_slot['path']), width, height, directory / 'pose-control.png',
            background_source=Path(semantic[0]['path']) if semantic else None)
        # 显式选择优先：线稿和深度图在缩略图里很难分辨，按分类猜会猜错；
        # auto 才回到老规则（分类里带「线稿」→ Lineart，其余 → Depth）。
        explicit = str(control_slot.get('control') or 'auto').strip().lower()
        if explicit == 'lineart':
            control_mode = 'Lineart'
        elif explicit == 'depth':
            control_mode = 'Depth'
        else:
            control_mode = 'Lineart' if '线稿' in control_slot.get('category', '') else 'Depth'
    graph_parts = [part for part in (
        '姿势按结构控制图执行' if control_path else '',
        prefix_text,
    ) if part]
    graph_prompt = (f'{"；".join(graph_parts)}。{prompt}' if graph_parts else prompt)
    # 风格锁只加在送模型的这一份前面：prompt 仍是用户原文，回看时能分清哪句是他写的。
    graph_prompt, applied_style = style_blocks.apply(graph_prompt, style_block)
    if control_path is not None and len(semantic) > 3:
        # Union 的参考编码最多吃 3 张图。以前这里用 semantic[1:3] 静默截断，
        # 提示词却仍会称呼「参考图4」，模型只能凭空编。宁可报错也不悄悄丢。
        dropped = '」「'.join(slot.get('label') or slot.get('role') or '参考图'
                             for slot in semantic[3:])
        raise ValueError(
            f'带姿势控制时最多 3 张参考图（当前 {len(semantic)} 张）：'
            f'请先去掉「{dropped}」中的一张，再用「继续改这张」补细节。')
    if control_path is not None and semantic:
        # Union 的参考编码用第一张图决定画幅，先裁到目标比例，避免补边变成侧栏；
        # 其余参考（造型/服装）作为 image_2、image_3 补充外观。
        main_reference = _crop_to_aspect(Path(semantic[0]['path']), width, height,
                                         directory / 'reference-main.png')
        recipe = qwen_union_control.graph(
            graph_prompt, output_prefix, seed, width, height,
            [main_reference, *[Path(slot['path']) for slot in semantic[1:3]]],
            control_path, control_mode=control_mode or 'Depth',
            control_strength=1.0, steps=steps, lora_names=_image_loras(creative_profile))
    else:
        recipe = text_graph(graph_prompt, output_prefix, seed, width, height,
                            None, steps, [Path(slot['path']) for slot in semantic] or None,
                            _image_loras(creative_profile),
                            refine=refine, control_image=control_path,
                            control_strength=CONTROL_STRENGTH)
    workflow = directory / 'workflow.json'
    workflow.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
    request = {'task_id': task_id, 'mode': 'reference' if slots else 'text',
               'engine': ('Qwen Image 2.1 · 参考图 + 姿势控制' if control_path and semantic else
                          'Qwen Image 2.1 · 多参考 + 姿势控制' if control_path else
                          'Qwen Image 2.1 · 多参考' if slots else 'Qwen Image 2.1'),
               'creative_profile': creative_profile, 'prompt': prompt, 'prompt_for_model': graph_prompt,
               'style_block': applied_style,
               'reference_prefix': prefix_text, 'references': slots,
               'control': ({'role': '姿势', 'source': control_slot['path'],
                            'path': str(control_path), 'mode': control_mode,
                            'strength': 1.0 if semantic else CONTROL_STRENGTH}
                           if control_path and control_slot else None),
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
    # 「耗时」要按 ComfyUI 自己的毫秒时间戳算，不能拿 updated_at 减 created_at：
    # 排队时间和事后人工核对回执的时间都不属于生成耗时（曾出现 21 秒的图显示 9 分 32 秒）。
    timing = h3_duration.history_timing(entry)
    if entry['status']['status_str'] == 'error':
        return {'status': 'error', 'raw_status': entry['status'], 'comfy_timing': timing}
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
    return {'status': 'success', 'output': str(output), 'width': frame.width, 'height': frame.height,
            'comfy_timing': timing}


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
