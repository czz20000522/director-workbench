from pathlib import Path
from backend import production_presets as presets


def touch(root: Path, relative: str, size: int = 8) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)


def test_model_catalog_lists_local_files_and_preset_usage(tmp_path):
    for name, category in (
        ('minimax_h3_fl2va_pruned-UD-Q3_K_XL.gguf', 'diffusion_models'),
        ('qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'text_encoders'),
        ('minimax_h3_video_vae_fp16.safetensors', 'vae'),
        ('minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors', 'loras'),
        ('wav2vec2_large_english_fp16.safetensors', 'audio_encoders'),
    ):
        touch(tmp_path, f'{category}/{name}')

    catalog = presets.model_catalog(tmp_path)
    assert catalog['download_enabled'] is False
    assert catalog['root'] == 'ComfyUI-Shared/models'
    assert len(catalog['files']) == 5
    assert catalog['categories']['diffusion_models']['files'] == 1
    h3 = next(item for item in catalog['files'] if item['filename'].startswith('minimax_h3_fl2va'))
    assert h3['role'] == 'diffusion_model'
    assert h3['precision'] == 'gguf'
    assert h3['size_bytes'] == 8
    assert 'h3-text-native-landscape' in h3['used_by']

    requirements = presets.model_requirements('h3-text-native-landscape', tmp_path)
    assert any(item['filename'] == h3['filename'] and item['present'] for item in requirements)
    assert any(item['filename'] == 'minimax_h3_video_vae_fp16.safetensors' for item in requirements)
