"""本机硬件档位：所有与显卡相关的生成参数集中在这里。

换卡之后最容易出现的问题不是跑不动，而是代码里还散落着上一代显卡的
`0.4MP` / `--reserve-vram 1.0` / `8 步`，看起来一切正常，实际白放着新硬件。
所以这里做三件事：

1. 用 nvidia-smi 探测真实显存与计算能力（工作台 venv 里没有 torch，不能 import）。
2. 按显存分档，给出分辨率、步数、预留显存等推荐值。
3. 所有下游模块只读这里，不再各自写死；并允许环境变量覆盖以便实测校准。

档位数值来自 2026-11-07 在 RTX 5070 Ti 16GB / cu130 / sm_120 上的实测，
详见 `使用说明.md`。图生视频 4 秒：快速 0.4MP/8步 135s、标准 0.6MP/8步 211s、
长采样对照 0.6MP/20步 381s（画质无可见提升，所以 `steps_quality` 只服务对照档）。
换机器时只改这个文件。
"""
from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

# 每个档位的默认参数。MP = 百万像素（H3 的 ResolutionSelector 单位）。
# compact 是 8GB 时代的历史档，只作为小显存机器的兜底，不再是默认值。
TIERS: dict[str, dict[str, Any]] = {
    'compact': {
        'label': '小显存（<10 GiB）',
        'video_megapixels': 0.4, 'fast_megapixels': 0.4, 'quality_megapixels': 0.4,
        'steps_standard': 8, 'steps_fast': 8, 'steps_quality': 20,
        'reserve_vram_gb': 1.0, 'sigma_shift_fast': 12.0,
    },
    'balanced': {
        'label': '主流（10–20 GiB）',
        'video_megapixels': 0.6, 'fast_megapixels': 0.4, 'quality_megapixels': 0.6,
        'steps_standard': 8, 'steps_fast': 8, 'steps_quality': 20,
        'reserve_vram_gb': 0.6, 'sigma_shift_fast': 10.0,
    },
    'high': {
        'label': '大显存（≥20 GiB）',
        'video_megapixels': 0.8, 'fast_megapixels': 0.6, 'quality_megapixels': 0.8,
        'steps_standard': 8, 'steps_fast': 8, 'steps_quality': 20,
        'reserve_vram_gb': 0.4, 'sigma_shift_fast': 10.0,
    },
}


@dataclass(frozen=True)
class HardwareProfile:
    key: str
    label: str
    gpu_name: str
    vram_gib: float
    compute_cap: str
    source: str
    video_megapixels: float
    fast_megapixels: float
    quality_megapixels: float
    steps_standard: int
    steps_fast: int
    steps_quality: int
    reserve_vram_gb: float
    sigma_shift_fast: float

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _detect_gpu() -> tuple[str, float, str, str]:
    """返回 (名称, 显存 GiB, 计算能力, 来源)。任何失败都退回 balanced，不抛异常。"""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,compute_cap',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=6, check=False,
        )
        stdout = result.stdout or ''
        line = stdout.strip().splitlines()[0] if stdout.strip() else ''
        parts = [item.strip() for item in line.split(',')]
        if result.returncode == 0 and len(parts) >= 3:
            return parts[0], round(float(parts[1]) / 1024, 1), parts[2], 'nvidia-smi'
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return '未探测到 NVIDIA GPU', 0.0, '未知', 'fallback'


def _override_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
        return value if 0.1 <= value <= 4.0 else default
    except ValueError:
        return default


def _override_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
        return value if 4 <= value <= 60 else default
    except ValueError:
        return default


@functools.lru_cache(maxsize=1)
def profile() -> HardwareProfile:
    """进程内只探测一次。环境变量用于实测校准或特殊机器，优先级最高。"""
    name, vram, cap, source = _detect_gpu()
    forced = os.environ.get('DIRECTOR_HARDWARE_PROFILE', '').strip().lower()
    if forced in TIERS:
        key, source = forced, f'override:{forced}'
    elif vram <= 0:
        # 探测不到时不要退回 8GB 档，否则又会出现「新卡跑旧配置」。
        key = 'balanced'
    elif vram < 10:
        key = 'compact'
    elif vram < 20:
        key = 'balanced'
    else:
        key = 'high'
    values = TIERS[key]
    return HardwareProfile(
        key=key, label=values['label'], gpu_name=name, vram_gib=vram,
        compute_cap=cap, source=source,
        video_megapixels=_override_float('DIRECTOR_VIDEO_MEGAPIXELS', values['video_megapixels']),
        fast_megapixels=_override_float('DIRECTOR_FAST_MEGAPIXELS', values['fast_megapixels']),
        quality_megapixels=_override_float('DIRECTOR_QUALITY_MEGAPIXELS', values['quality_megapixels']),
        steps_standard=_override_int('DIRECTOR_STEPS_STANDARD', values['steps_standard']),
        steps_fast=_override_int('DIRECTOR_STEPS_FAST', values['steps_fast']),
        steps_quality=_override_int('DIRECTOR_STEPS_QUALITY', values['steps_quality']),
        reserve_vram_gb=_override_float('DIRECTOR_RESERVE_VRAM', values['reserve_vram_gb']),
        sigma_shift_fast=_override_float('DIRECTOR_FAST_SIGMA_SHIFT', values['sigma_shift_fast']),
    )


def video_megapixels(preset_kind: str = 'standard') -> float:
    current = profile()
    return {
        'fast': current.fast_megapixels,
        'quality': current.quality_megapixels,
        'standard': current.video_megapixels,
    }.get(preset_kind, current.video_megapixels)


def steps(preset_kind: str = 'standard') -> int:
    current = profile()
    return {
        'fast': current.steps_fast,
        'quality': current.steps_quality,
        'standard': current.steps_standard,
    }.get(preset_kind, current.steps_standard)


def comfy_reserve_vram() -> str:
    return f'{profile().reserve_vram_gb:.2f}'
