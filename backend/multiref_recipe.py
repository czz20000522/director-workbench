"""H3 multi-reference first-frame -> image-to-video recipe.

The recipe is deliberately explicit:

* the caller supplies an ordered list of character reference image names,
  an optional scene image name, the shot prompt, duration and aspect ratio;
* the scene image (or the first character reference when no scene is given) is
  Picture 1 for :class:`MiniMaxH3StillConditioningT8`; the remaining character
  references are its ``ref_images`` (at most eight);
* the still stage samples one explicit H3 Ref2VA first frame, then
  :class:`MiniMaxH3ImageToVideo` turns that frame into an H3 video;
* no cloud API node, model download, automatic reference choice or automatic
  shot splitting is performed.

``build_graph`` is a pure graph builder. ``prepare`` validates the graph against
a ComfyUI ``/object_info`` response (live by default, injectable for tests) and
returns the API prompt plus facts. ``submit_prompt`` / ``wait_for_prompt`` are
small helpers for an explicit one-shot real execution; they never retry or
silently switch recipes.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
RECIPE_ID = 'h3-multiref-first-frame'

COMFY_URL = os.environ.get('DIRECTOR_COMFY_URL', 'http://127.0.0.1:8188').rstrip('/')

STILL_UNET = 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'
VIDEO_UNET = 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'
CLIP_MODEL = 'qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors'
VIDEO_VAE = 'minimax_h3_video_vae_fp16.safetensors'
AUDIO_VAE = 'minimax_h3_audio_vae_fp32.safetensors'

ASPECT_SIZES = {'16:9': (1344, 768), '9:16': (768, 1344), '1:1': (768, 768)}
MIN_DURATION = 4.0
MAX_DURATION = 15.0
MAX_REF_IMAGES = 8
MAX_PICTURES = 9
DEFAULT_STILL_STEPS = 20
DEFAULT_VIDEO_STEPS = 20
DEFAULT_STILL_TARGET = 'direct_1_frame'
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
FORBIDDEN_NODE_PREFIXES = ('ComfyCloud',)

# Local node names are part of the recipe contract. They must be present in the
# live ComfyUI object_info and must not be API nodes.
REQUIRED_NODES = (
    'LoadImage',
    'UNETLoader',
    'CLIPLoader',
    'VAELoader',
    'MiniMaxH3StillConditioningT8',
    'MiniMaxH3DualClockSamplerT8',
    'MiniMaxH3StillDecodeT8',
    'MiniMaxH3ImageToVideo',
    'BasicGuider',
    'RandomNoise',
    'SamplerCustomAdvanced',
    'VAEDecode',
    'VAEDecodeAudio',
    'CreateVideo',
    'SaveVideo',
)

NODE_REQUIRED_INPUTS = {
    'LoadImage': {'image'},
    'UNETLoader': {'unet_name'},
    'CLIPLoader': {'clip_name'},
    'VAELoader': {'vae_name'},
    'MiniMaxH3StillConditioningT8': {
        'clip', 'video_vae', 'prompt', 'canvas_mode', 'width', 'height',
        'target_mode', 'reference_strength', 'audio_target', 'strict_prompt_tags',
        'ref_image_size', 'edit_image',
    },
    'MiniMaxH3DualClockSamplerT8': {'model', 'av_latent', 'steps', 'shift_video', 'shift_audio'},
    'MiniMaxH3StillDecodeT8': {'av_latent', 'video_vae', 'frame_selection', 'frame_index'},
    'MiniMaxH3ImageToVideo': {'clip', 'vae', 'prompt', 'width', 'height', 'length'},
    'BasicGuider': {'model', 'conditioning'},
    'RandomNoise': {'noise_seed'},
    'SamplerCustomAdvanced': {'noise', 'guider', 'sampler', 'sigmas', 'latent_image'},
    'VAEDecode': {'samples', 'vae'},
    'VAEDecodeAudio': {'samples', 'vae'},
    'CreateVideo': {'images', 'fps'},
    'SaveVideo': {'video', 'filename_prefix', 'format'},
}

MODEL_OPTIONS = {
    'UNETLoader': {'unet_name': (STILL_UNET, VIDEO_UNET)},
    'CLIPLoader': {'clip_name': (CLIP_MODEL,)},
    'VAELoader': {'vae_name': (VIDEO_VAE, AUDIO_VAE)},
}


class MultiRefError(RuntimeError):
    """Structured request/environment/execution failure."""

    def __init__(self, code: str, message: str, status_code: int = 422, *,
                 blockers: list[dict[str, Any]] | None = None, **details: Any):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.blockers = list(blockers or [])
        self.details = details

    @property
    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'code': self.code,
            'message': str(self),
            'retryable': self.status_code in {409, 502, 503, 504},
            **self.details,
        }
        if self.blockers:
            payload['blockers'] = self.blockers
        return payload


class MultiRefRequest(BaseModel):
    """Explicit inputs; no image list means the caller supplied nothing."""

    model_config = ConfigDict(extra='forbid')
    character_ref_images: list[str]
    scene_image: str | None = None
    prompt: str
    duration_seconds: float
    aspect_ratio: str
    seed: int = Field(default=42, ge=0, le=2147483647)
    still_steps: int = Field(default=DEFAULT_STILL_STEPS, ge=1, le=100)
    video_steps: int = Field(default=DEFAULT_VIDEO_STEPS, ge=1, le=100)
    output_prefix: str = 'director-workbench/multiref'


def _blocker(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {'code': code, 'message': message, **details}


def _valid_image_name(name: Any) -> bool:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        return False
    if name in {'.', '..'} or any(char in name for char in '/\\:\x00') or '..' in name:
        return False
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def _snap_frames(duration_seconds: float) -> tuple[int, int, float]:
    requested = max(5, round(float(duration_seconds) * 24))
    frames = requested + (5 - requested % 17) % 17
    return requested, frames, round(frames / 24, 3)


def validate_request(request: MultiRefRequest) -> dict[str, Any]:
    """Validate and freeze the explicit request into a graph plan."""
    blockers: list[dict[str, Any]] = []
    refs = list(request.character_ref_images or [])
    if not refs:
        blockers.append(_blocker(
            'reference_images_required',
            '至少需要一张角色参考图；没有参考图时不能构造多参考首帧。',
            required='character_ref_images',
        ))
    for index, name in enumerate(refs):
        if not _valid_image_name(name):
            blockers.append(_blocker(
                'invalid_reference_image',
                f'第 {index + 1} 张角色参考图不是有效的 ComfyUI 输入图片名。',
                index=index, value=name,
            ))
    scene = request.scene_image.strip() if isinstance(request.scene_image, str) else request.scene_image
    if scene is not None and not _valid_image_name(scene):
        blockers.append(_blocker(
            'invalid_scene_image',
            '场景图不是有效的 ComfyUI 输入图片名。',
            value=request.scene_image,
        ))
    if not isinstance(request.prompt, str) or not request.prompt.strip():
        blockers.append(_blocker('prompt_required', '分镜 prompt 不能为空。'))
    if not MIN_DURATION <= float(request.duration_seconds) <= MAX_DURATION:
        blockers.append(_blocker(
            'invalid_duration',
            f'H3 时长必须在 {MIN_DURATION:g}–{MAX_DURATION:g} 秒之间。',
            value=request.duration_seconds,
        ))
    if request.aspect_ratio not in ASPECT_SIZES:
        blockers.append(_blocker(
            'invalid_aspect_ratio',
            '画幅必须是 16:9、9:16 或 1:1。',
            value=request.aspect_ratio,
            options=list(ASPECT_SIZES),
        ))
    primary = scene if scene is not None else (refs[0] if refs else None)
    ref_images = refs if scene is not None else refs[1:]
    if len(ref_images) > MAX_REF_IMAGES:
        blockers.append(_blocker(
            'too_many_reference_images',
            f'edit_image 之外最多 {MAX_REF_IMAGES} 张参考图；当前为 {len(ref_images)} 张。',
            value=len(ref_images), max=MAX_REF_IMAGES,
        ))
    if primary is not None and len(ref_images) + 1 > MAX_PICTURES:
        blockers.append(_blocker(
            'too_many_reference_images',
            f'总参考图最多 {MAX_PICTURES} 张（edit_image + ref_images）。',
            value=len(ref_images) + 1, max=MAX_PICTURES,
        ))
    prefix = request.output_prefix.strip() if isinstance(request.output_prefix, str) else ''
    if not prefix or '\x00' in prefix or '..' in prefix.replace('\\', '/').split('/'):
        blockers.append(_blocker(
            'invalid_output_prefix',
            '输出前缀必须是非空、不含 .. 的相对前缀。',
            value=request.output_prefix,
        ))
    if blockers:
        first = blockers[0]
        raise MultiRefError(first['code'], first['message'], 422, blockers=blockers)
    requested_frames, frames, actual_duration = _snap_frames(request.duration_seconds)
    width, height = ASPECT_SIZES[request.aspect_ratio]
    return {
        'primary_image': primary,
        'ref_images': ref_images,
        'picture_count': len(ref_images) + 1,
        'width': width,
        'height': height,
        'aspect_ratio': request.aspect_ratio,
        'requested_frames': requested_frames,
        'frames': frames,
        'actual_duration_seconds': actual_duration,
        'prompt': request.prompt,
        'seed': request.seed,
        'still_steps': request.still_steps,
        'video_steps': request.video_steps,
        'output_prefix': prefix,
    }


def _load_image_node(name: str) -> dict[str, Any]:
    return {'class_type': 'LoadImage', 'inputs': {'image': name}}


def _link(node_id: str, index: int) -> list[Any]:
    return [node_id, index]


def build_graph(request: MultiRefRequest) -> dict[str, Any]:
    """Build one API-format graph; no network, model loading or file writes."""
    plan = validate_request(request)
    graph: dict[str, Any] = {
        'load_primary': _load_image_node(plan['primary_image']),
        'still_unet': {'class_type': 'UNETLoader',
                       'inputs': {'unet_name': STILL_UNET, 'weight_dtype': 'default'}},
        'video_unet': {'class_type': 'UNETLoader',
                       'inputs': {'unet_name': VIDEO_UNET, 'weight_dtype': 'default'}},
        'clip': {'class_type': 'CLIPLoader',
                 'inputs': {'clip_name': CLIP_MODEL, 'type': 'minimax', 'device': 'default'}},
        'vae_video': {'class_type': 'VAELoader', 'inputs': {'vae_name': VIDEO_VAE}},
        'vae_audio': {'class_type': 'VAELoader', 'inputs': {'vae_name': AUDIO_VAE}},
        'still': {
            'class_type': 'MiniMaxH3StillConditioningT8',
            'inputs': {
                'clip': _link('clip', 0),
                'video_vae': _link('vae_video', 0),
                'prompt': plan['prompt'],
                'canvas_mode': 'custom',
                'width': plan['width'],
                'height': plan['height'],
                'target_mode': DEFAULT_STILL_TARGET,
                'reference_strength': 0.999,
                'audio_target': 'generate_and_discard',
                'strict_prompt_tags': True,
                'ref_image_size': 'match',
                'edit_image': _link('load_primary', 0),
            },
        },
        'still_clock': {
            'class_type': 'MiniMaxH3DualClockSamplerT8',
            'inputs': {
                'model': _link('still_unet', 0),
                'av_latent': _link('still', 1),
                'steps': plan['still_steps'],
                'shift_video': 12.0,
                'shift_audio': 3.0,
                'sampler_name': 'dual_clock_euler',
                'scheduler': 'native_flow',
            },
        },
        'still_guider': {'class_type': 'BasicGuider',
                         'inputs': {'model': _link('still_clock', 0),
                                    'conditioning': _link('still', 0)}},
        'still_noise': {'class_type': 'RandomNoise',
                        'inputs': {'noise_seed': plan['seed']}},
        'still_sample': {
            'class_type': 'SamplerCustomAdvanced',
            'inputs': {
                'noise': _link('still_noise', 0),
                'guider': _link('still_guider', 0),
                'sampler': _link('still_clock', 1),
                'sigmas': _link('still_clock', 2),
                'latent_image': _link('still', 1),
            },
        },
        'still_decode': {
            'class_type': 'MiniMaxH3StillDecodeT8',
            'inputs': {
                'av_latent': _link('still_sample', 0),
                'video_vae': _link('vae_video', 0),
                'frame_selection': 'first',
                'frame_index': 0,
            },
        },
        'i2v': {
            'class_type': 'MiniMaxH3ImageToVideo',
            'inputs': {
                'clip': _link('clip', 0),
                'vae': _link('vae_video', 0),
                'prompt': plan['prompt'],
                'width': plan['width'],
                'height': plan['height'],
                'length': plan['frames'],
                'first_frame': _link('still_decode', 0),
            },
        },
        'video_clock': {
            'class_type': 'MiniMaxH3DualClockSamplerT8',
            'inputs': {
                'model': _link('video_unet', 0),
                'av_latent': _link('i2v', 1),
                'steps': plan['video_steps'],
                'shift_video': 12.0,
                'shift_audio': 3.0,
                'sampler_name': 'dual_clock_euler',
                'scheduler': 'native_flow',
            },
        },
        'video_guider': {'class_type': 'BasicGuider',
                         'inputs': {'model': _link('video_clock', 0),
                                    'conditioning': _link('i2v', 0)}},
        'video_noise': {'class_type': 'RandomNoise',
                        'inputs': {'noise_seed': plan['seed']}},
        'video_sample': {
            'class_type': 'SamplerCustomAdvanced',
            'inputs': {
                'noise': _link('video_noise', 0),
                'guider': _link('video_guider', 0),
                'sampler': _link('video_clock', 1),
                'sigmas': _link('video_clock', 2),
                'latent_image': _link('i2v', 1),
            },
        },
        'video_decode': {'class_type': 'VAEDecode',
                         'inputs': {'samples': _link('video_sample', 0),
                                    'vae': _link('vae_video', 0)}},
        'audio_decode': {'class_type': 'VAEDecodeAudio',
                         'inputs': {'samples': _link('video_sample', 0),
                                    'vae': _link('vae_audio', 0)}},
        'create_video': {
            'class_type': 'CreateVideo',
            'inputs': {
                'images': _link('video_decode', 0),
                'fps': 24.0,
                'audio': _link('audio_decode', 0),
                'bit_depth': 8,
                'color_space': 'sRGB',
            },
        },
        'save_video': {
            'class_type': 'SaveVideo',
            'inputs': {
                'video': _link('create_video', 0),
                'filename_prefix': plan['output_prefix'],
                'format': 'auto',
                'format.codec': 'auto',
                'codec': 'auto',
                'video-preview': '',
            },
        },
    }
    for index, name in enumerate(plan['ref_images']):
        graph[f'load_ref_{index}'] = _load_image_node(name)
        graph['still']['inputs'][f'ref_images.ref_image_{index}'] = _link(f'load_ref_{index}', 0)
    return graph


def _input_names(info: dict[str, Any]) -> set[str]:
    required = info.get('input', {}).get('required', {}) if isinstance(info, dict) else {}
    return set(required) if isinstance(required, dict) else set()


def _option_values(info: dict[str, Any], key: str) -> list[Any]:
    value = (info.get('input', {}).get('required', {}) or {}).get(key)
    if not isinstance(value, list) or not value:
        return []
    options = value[0]
    return options if isinstance(options, list) else []


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) and not isinstance(value[1], bool)


def environment_facts(object_info: dict[str, Any]) -> dict[str, Any]:
    """Return node/model availability facts without choosing or executing anything."""
    blockers: list[dict[str, Any]] = []
    nodes: dict[str, Any] = {}
    for name in REQUIRED_NODES:
        info = object_info.get(name)
        available = isinstance(info, dict)
        api_node = bool(info.get('api_node')) if available else False
        nodes[name] = {'available': available, 'api_node': api_node}
        if not available:
            blockers.append(_blocker('node_missing', f'ComfyUI 缺少节点 {name}。', node=name))
        elif api_node:
            blockers.append(_blocker('cloud_api_node_forbidden',
                                     f'{name} 是 API/云节点；本配方只允许本地节点。', node=name))
    models: dict[str, Any] = {}
    for node_name, fields in MODEL_OPTIONS.items():
        info = object_info.get(node_name)
        for field, expected in fields.items():
            options = _option_values(info, field) if isinstance(info, dict) else []
            for model in expected:
                available = model in options
                models[model] = {'loader': node_name, 'field': field,
                                 'available': available, 'options': options}
                if not available:
                    blockers.append(_blocker(
                        'model_missing', f'ComfyUI 未登记模型 {model}（{node_name}.{field}）。',
                        node=node_name, field=field, model=model, options=options,
                    ))
    return {
        'schema_version': SCHEMA_VERSION,
        'ready': not blockers,
        'nodes': nodes,
        'models': models,
        'blockers': blockers,
        'limits': {
            'max_ref_images': MAX_REF_IMAGES,
            'max_pictures': MAX_PICTURES,
            'duration_seconds': {'min': MIN_DURATION, 'max': MAX_DURATION},
            'aspect_sizes': {key: {'width': value[0], 'height': value[1]}
                             for key, value in ASPECT_SIZES.items()},
        },
        'execution': {
            'still_target': DEFAULT_STILL_TARGET,
            'fps': 24.0,
            'local_nodes_only': True,
            'cloud_api': False,
        },
    }


def validate_graph(graph: dict[str, Any], object_info: dict[str, Any]) -> dict[str, Any]:
    """Validate graph node types, links, required inputs and local models."""
    facts = environment_facts(object_info)
    blockers = list(facts['blockers'])
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            blockers.append(_blocker('invalid_graph_node', f'节点 {node_id} 不是对象。', node=node_id))
            continue
        class_type = node.get('class_type')
        info = object_info.get(class_type) if isinstance(class_type, str) else None
        if isinstance(class_type, str) and class_type.startswith(FORBIDDEN_NODE_PREFIXES):
            blockers.append(_blocker('cloud_api_node_forbidden',
                                     f'节点 {node_id} 使用禁止的云 API 类型 {class_type}。', node=node_id))
        if not isinstance(info, dict):
            blockers.append(_blocker('node_missing', f'节点 {node_id} 的类型 {class_type} 不可用。',
                                     node=node_id, class_type=class_type))
            continue
        required = NODE_REQUIRED_INPUTS.get(str(class_type), set())
        inputs = node.get('inputs') if isinstance(node.get('inputs'), dict) else {}
        missing = sorted(required - set(inputs))
        if missing:
            blockers.append(_blocker('node_input_missing',
                                     f'节点 {node_id} 缺少必填输入: {", ".join(missing)}',
                                     node=node_id, missing=missing))
        output_count = len(info.get('output') or [])
        for key, value in inputs.items():
            if not _is_link(value):
                continue
            target_id, output_index = value
            target = graph.get(target_id)
            if target is None:
                blockers.append(_blocker('link_target_missing',
                                         f'节点 {node_id}.{key} 指向不存在的节点 {target_id}。',
                                         node=node_id, input=key, target=target_id))
                continue
            target_info = object_info.get(target.get('class_type'))
            target_outputs = len(target_info.get('output') or []) if isinstance(target_info, dict) else 0
            if output_index >= target_outputs:
                blockers.append(_blocker('link_output_missing',
                                         f'节点 {node_id}.{key} 的输出索引 {output_index} 越界。',
                                         node=node_id, input=key, target=target_id, index=output_index))
        if class_type == 'LoadImage':
            image = inputs.get('image')
            options = _option_values(info, 'image')
            if options and image not in options:
                blockers.append(_blocker('reference_image_not_registered',
                                         f'输入图片 {image!r} 不在 ComfyUI 当前 LoadImage 列表中。',
                                         node=node_id, image=image))
    if blockers:
        first = blockers[0]
        raise MultiRefError(first['code'], first['message'], 503, blockers=blockers)
    return facts


def fetch_object_info(base_url: str = COMFY_URL, *, timeout: float = 30.0) -> dict[str, Any]:
    url = base_url.rstrip('/') + '/object_info'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise MultiRefError(
            'comfy_unavailable', f'无法读取 ComfyUI /object_info: {exc}', 503,
            blockers=[_blocker('comfy_unavailable', f'{url} 不可访问或返回非 JSON。', url=url)],
        ) from exc
    if not isinstance(data, dict):
        raise MultiRefError('comfy_unavailable', 'ComfyUI /object_info 返回的不是对象。', 503)
    return data


def prepare(request: MultiRefRequest, *,
            object_info: dict[str, Any] | None = None,
            base_url: str = COMFY_URL) -> dict[str, Any]:
    """Build and validate one graph; returns prompt, frozen plan and env facts."""
    plan = validate_request(request)
    graph = build_graph(request)
    info = object_info if object_info is not None else fetch_object_info(base_url)
    facts = validate_graph(graph, info)
    return {
        'schema_version': SCHEMA_VERSION,
        'recipe': RECIPE_ID,
        'prompt': graph,
        'metadata': {
            **plan,
            'primary_role': 'scene' if request.scene_image is not None else 'first_character_reference',
            'reference_order': list(request.character_ref_images),
            'cloud_api': False,
            'local_nodes_only': True,
        },
        'environment': facts,
    }


def submit_prompt(graph: dict[str, Any], *, base_url: str = COMFY_URL,
                  client_id: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Submit exactly one prompt; no retry and no recipe fallback."""
    payload = {
        'prompt': graph,
        'client_id': client_id or f'director-workbench-multiref-{uuid.uuid4().hex[:12]}',
    }
    request = urllib.request.Request(
        base_url.rstrip('/') + '/prompt',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')[:4000]
        raise MultiRefError(
            'comfy_prompt_rejected', f'ComfyUI /prompt 返回 HTTP {exc.code}。', 502,
            blockers=[_blocker('comfy_prompt_rejected', body or f'HTTP {exc.code}',
                               http_status=exc.code)],
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise MultiRefError('comfy_unavailable', f'ComfyUI /prompt 请求失败: {exc}', 503) from exc
    if not isinstance(data, dict):
        raise MultiRefError('comfy_prompt_rejected', 'ComfyUI /prompt 未返回对象。', 502)
    if data.get('node_errors'):
        raise MultiRefError(
            'comfy_prompt_rejected', 'ComfyUI 拒绝了图节点。', 502,
            blockers=[_blocker('comfy_node_errors', '节点校验失败。', node_errors=data['node_errors'])],
        )
    if not data.get('prompt_id'):
        raise MultiRefError('comfy_prompt_rejected', 'ComfyUI /prompt 未返回 prompt_id。', 502,
                            response=data)
    return data


def prompt_history(prompt_id: str, *, base_url: str = COMFY_URL, timeout: float = 30.0) -> dict[str, Any]:
    url = base_url.rstrip('/') + '/history/' + urllib.parse.quote(prompt_id, safe='')
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise MultiRefError('comfy_unavailable', f'无法读取 ComfyUI history: {exc}', 503) from exc
    if not isinstance(data, dict):
        raise MultiRefError('comfy_invalid_history', 'ComfyUI history 未返回对象。', 502)
    return {'prompt_id': prompt_id, 'entry': data.get(prompt_id), 'raw': data}


def wait_for_prompt(prompt_id: str, *, base_url: str = COMFY_URL,
                    timeout: float = 600.0, poll_seconds: float = 2.0) -> dict[str, Any]:
    """Poll one prompt's history; never resubmit it."""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        result = prompt_history(prompt_id, base_url=base_url)
        if result['entry']:
            return result
        time.sleep(max(0.25, poll_seconds))
    raise MultiRefError('comfy_prompt_timeout', f'等待 prompt {prompt_id} 超时。', 504,
                        prompt_id=prompt_id, timeout_seconds=timeout)
