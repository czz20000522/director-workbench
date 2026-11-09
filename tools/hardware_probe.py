"""给 PowerShell / 外部脚本读取硬件档位，避免启动脚本再写死显存参数。

刻意用 importlib 按文件路径加载：不触发 `backend/__init__.py`，也不依赖
工作台 venv 里有没有装第三方库，任何 Python 3.10+ 都能跑。

    python tools/hardware_probe.py reserve-vram   → 0.60
    python tools/hardware_probe.py megapixels     → 0.6
    python tools/hardware_probe.py profile        → balanced
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'backend' / 'hardware.py'
spec = importlib.util.spec_from_file_location('_director_hardware', SOURCE)
if spec is None or spec.loader is None:
    raise SystemExit('无法加载 backend/hardware.py')
module = importlib.util.module_from_spec(spec)
# dataclass 在处理类型注解时会查 sys.modules[cls.__module__]，按路径加载必须自己注册。
sys.modules[spec.name] = module
spec.loader.exec_module(module)

key = sys.argv[1] if len(sys.argv) > 1 else 'profile'
if key == 'reserve-vram':
    print(module.comfy_reserve_vram())
elif key == 'megapixels':
    print(module.video_megapixels())
elif key == 'profile':
    print(module.profile().key)
else:
    raise SystemExit(f'未知查询：{key}')
