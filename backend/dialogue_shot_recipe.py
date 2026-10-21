"""One-shot dialogue pipeline: IndexTTS voice -> H3 audio-conditioned video.

The speech stage is intentionally part of the shot recipe, not a separate
public task.  This module only builds the second stage (the H3 graph); the
orchestrator owns the speech stage and the single task receipt.
"""
from __future__ import annotations

from typing import Any

RECIPE_ID = 'h3-dialogue-shot-v1'
DEFAULT_WIDTH = 864
DEFAULT_HEIGHT = 480
DEFAULT_STEPS = 8
AUDIO_CONDITION_NODE = 'MiniMaxH3AudioConditioningT8'
AUDIO_WINDOW_NODE = 'MiniMaxH3AudioWindowT8'
SAMPLER_NODE = 'MiniMaxH3DualClockSamplerT8'
DECODE_NODE = 'MiniMaxH3AVDecodeT8'
TRIM_NODE = 'MiniMaxH3OutputTrimT8'

VIDEO_UNET = 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'
CLIP_MODEL = 'qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors'
VIDEO_VAE = 'minimax_h3_video_vae_fp16.safetensors'
AUDIO_VAE = 'minimax_h3_audio_vae_fp32.safetensors'


def build_graph(
    *,
    first_frame_name: str,
    audio_name: str,
    prompt: str,
    duration_seconds: float,
    seed: int,
    output_prefix: str,
    reference_names: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    steps: int = DEFAULT_STEPS,
) -> dict[str, Any]:
    """Build the frozen H3 API graph for one already-existing dialogue WAV."""
    if not first_frame_name or not audio_name:
        raise ValueError('first_frame_name and audio_name are required')
    if not 4.0 <= float(duration_seconds) <= 15.0:
        raise ValueError('duration_seconds must be between 4 and 15')
    refs = list(reference_names or [])
    graph: dict[str, Any] = {
        'audio': {'class_type': 'LoadAudio', 'inputs': {'audio': audio_name}},
        'window': {
            'class_type': AUDIO_WINDOW_NODE,
            'inputs': {
                'audio': ['audio', 0],
                'scene_start_seconds': 0.0,
                'scene_duration_seconds': float(duration_seconds),
                'warmup_seconds': 0.0,
                'cooldown_seconds': 0.0,
                'ensure_minimum_context': True,
            },
        },
        'first_frame': {'class_type': 'LoadImage', 'inputs': {'image': first_frame_name}},
        'clip': {'class_type': 'CLIPLoader', 'inputs': {
            'clip_name': CLIP_MODEL, 'type': 'minimax', 'device': 'default'}},
        'vae_video': {'class_type': 'VAELoader', 'inputs': {'vae_name': VIDEO_VAE}},
        'vae_audio': {'class_type': 'VAELoader', 'inputs': {'vae_name': AUDIO_VAE}},
        'model': {'class_type': 'UNETLoader', 'inputs': {
            'unet_name': VIDEO_UNET, 'weight_dtype': 'default'}},
        'conditioning': {
            'class_type': AUDIO_CONDITION_NODE,
            'inputs': {
                'clip': ['clip', 0],
                'video_vae': ['vae_video', 0],
                'audio_vae': ['vae_audio', 0],
                'prompt': prompt,
                'width': int(width),
                'height': int(height),
                'length': ['window', 1],
                'task_type': 'auto',
                'audio_mode': 'lock_source',
                'audio_denoise_strength': 0.0,
                'add_source_as_reference': True,
                'prompt_primary_audio_ordinal': 1,
                'strict_prompt_tags': True,
                'ref_image_size': 'match',
                'reference_video_policy': 'official_2_to_15s',
                'drive_audio': ['window', 0],
                'final_audio': ['window', 0],
                'first_frame': ['first_frame', 0],
            },
        },
        'sampler': {
            'class_type': SAMPLER_NODE,
            'inputs': {
                'model': ['model', 0],
                'av_latent': ['conditioning', 1],
                'steps': int(steps),
                'shift_video': 12.0,
                'shift_audio': 3.0,
                'sampler_name': 'dual_clock_euler',
                'scheduler': 'native_flow',
            },
        },
        'guider': {
            'class_type': 'BasicGuider',
            'inputs': {'model': ['sampler', 0], 'conditioning': ['conditioning', 0]},
        },
        'noise': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': int(seed)}},
        'sample': {
            'class_type': 'SamplerCustomAdvanced',
            'inputs': {
                'noise': ['noise', 0],
                'guider': ['guider', 0],
                'sampler': ['sampler', 1],
                'sigmas': ['sampler', 2],
                'latent_image': ['conditioning', 1],
            },
        },
        'decode': {
            'class_type': DECODE_NODE,
            'inputs': {
                'av_latent': ['sample', 0],
                'video_vae': ['vae_video', 0],
                'audio_vae': ['vae_audio', 0],
            },
        },
        'trim': {
            'class_type': TRIM_NODE,
            'inputs': {
                'frames': ['decode', 0],
                'start_seconds': ['window', 2],
                'duration_seconds': ['window', 3],
                'audio': ['conditioning', 2],
                'fps': 24.0,
            },
        },
        'video': {
            'class_type': 'CreateVideo',
            'inputs': {
                'images': ['trim', 0],
                'fps': 24.0,
                'audio': ['trim', 1],
                'bit_depth': 8,
                'color_space': 'sRGB',
            },
        },
        'save': {
            'class_type': 'SaveVideo',
            'inputs': {
                'video': ['video', 0],
                'filename_prefix': output_prefix,
                'format': 'auto',
                'format.codec': 'auto',
                'codec': 'auto',
                'video-preview': '',
            },
        },
    }
    for index, name in enumerate(refs):
        node_id = f'ref_{index}'
        graph[node_id] = {'class_type': 'LoadImage', 'inputs': {'image': name}}
        graph['conditioning']['inputs'][f'ref_images.ref_image_{index}'] = [node_id, 0]
    return graph
