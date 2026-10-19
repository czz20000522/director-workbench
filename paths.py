"""Single source of truth for the filesystem roots this project depends on.

Two machine roots, deliberately split so the external ComfyUI installation and
this product's own data never share a directory:

* ``COMFY_ROOT`` — the ComfyUI Desktop data root: engine install, shared media,
  models and caches. Owned by the external ComfyUI service; uninstalling or
  reinstalling ComfyUI may replace everything below it.
* ``DATA_ROOT`` — this product's data root: the storage configuration, runtime
  state (database, accounts, recovery logs) and the local production workspace.
  Owned by the workbench; it must survive ComfyUI removal or relocation.

Machine roots sit outside this checkout and predate it, so relocating the
checkout must leave them alone. Repository roots follow the checkout.

Resolution is environment override first, then the recorded default, so a
checkout can be exercised without the ComfyUI environment present. No
configuration file is read here: effective asset roots stay exclusively in
``DATA_ROOT/config/storage.json``.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _override(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else default


def storage_config(root: Path | None = None) -> Path:
    """The workbench's effective-storage config, resolved against a data root.

    It takes the root as an argument because callers legitimately run against a
    substitute data root — tests build a synthetic one, and private layouts swap
    the root at runtime.  Deriving it from the environment instead would silently
    ignore that substitution and read the real host's asset mapping.
    """
    return (DATA_ROOT if root is None else Path(root)) / 'config/storage.json'


# --- ComfyUI Desktop machine root: external service, never our data ----------
COMFY_ROOT = _override('DIRECTOR_COMFY_ROOT', Path('D:/Comfy-Desktop'))
SHARED_ROOT = COMFY_ROOT / 'ComfyUI-Shared'
INSTALL_ROOT = COMFY_ROOT / 'ComfyUI-Installs/第一个comfyui配置/ComfyUI'
COMFY_PYTHON = INSTALL_ROOT / '.venv/Scripts/python.exe'
LLAMA_ROOT = _override('DIRECTOR_LLAMA_ROOT', Path('D:/software/llama.cpp'))

# --- Workbench machine root: our data, independent of ComfyUI -----------------
DATA_ROOT = _override(
    'DIRECTOR_DATA_ROOT',
    Path('D:/software/JetBrains/python_workspace/director-workbench-data'),
)
RUNTIME_ROOT = DATA_ROOT / 'runtime'
CONFIG_ROOT = DATA_ROOT / '.config/director-workbench'

# --- Repository roots --------------------------------------------------------
# The checkout is a directory of its own, so the Git root is the checkout.
REPO_ROOT = _override('DIRECTOR_REPO_ROOT', PROJECT_ROOT)
SKILLS_ROOT = REPO_ROOT / '.agents'

# storage_layout.py is published, so the checkout's copy is the one that runs.
STORAGE_LAYOUT = PROJECT_ROOT / 'production/storage_layout.py'
