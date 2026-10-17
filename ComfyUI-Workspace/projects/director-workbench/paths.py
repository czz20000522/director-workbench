"""Single source of truth for the filesystem roots this project depends on.

Two kinds of root, deliberately kept apart:

* Machine roots — the ComfyUI installation, the shared media/tool tree, the host
  configuration directory, the runtime directory and the local inference tools.
  They sit outside this checkout and predate it, so relocating the checkout must
  leave them alone.
* Repository roots — the checkout itself and the in-repo skill and production
  directories.  Every published file hangs off these, so they follow the checkout.

Callers import a resolved value instead of writing a literal.  A literal that
reaches into a machine root is a relocation bug waiting to happen: nothing fails
until the checkout moves.  A ``Path(__file__).resolve().parents[N]`` expression is
worse, because it keeps working — it just points at a neighbouring directory.

Resolution is environment override first, then the recorded default, so a checkout
can be exercised without the ComfyUI environment present.  No configuration file is
read here: effective asset roots stay exclusively in
``ComfyUI-Workspace/config/storage.json``.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _override(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else default


WORKSPACE_DIRNAME = 'ComfyUI-Workspace'


def storage_config(root: Path | None = None) -> Path:
    """The host's effective-storage config, resolved against a given host root.

    It takes the root as an argument because callers legitimately run against a
    substitute host — tests build a synthetic one, and private layouts swap the
    root at runtime.  Deriving it from the machine root instead would silently
    ignore that substitution and read the real host's asset mapping.
    """
    return (DESKTOP_ROOT if root is None else Path(root)) / WORKSPACE_DIRNAME / 'config/storage.json'


# --- Machine roots: explicit, never derived from the checkout's depth ---------
DESKTOP_ROOT = _override('DIRECTOR_DESKTOP_ROOT', Path('D:/Comfy-Desktop'))
SHARED_ROOT = DESKTOP_ROOT / 'ComfyUI-Shared'
WORKSPACE_ROOT = DESKTOP_ROOT / WORKSPACE_DIRNAME
RUNTIME_ROOT = WORKSPACE_ROOT / 'runtime'
CONFIG_ROOT = DESKTOP_ROOT / '.config' / 'director-workbench'
INSTALL_ROOT = DESKTOP_ROOT / 'ComfyUI-Installs/第一个comfyui配置/ComfyUI'
COMFY_PYTHON = INSTALL_ROOT / '.venv/Scripts/python.exe'
LLAMA_ROOT = _override('DIRECTOR_LLAMA_ROOT', Path('D:/software/llama.cpp'))

# --- Repository roots --------------------------------------------------------
# The checkout currently lives inside the ComfyUI host directory, so the Git root
# is two levels above it.  Once the checkout is relocated to a directory of its
# own, REPO_ROOT becomes PROJECT_ROOT and only this default changes; SKILLS_ROOT
# follows, because the published skill tree moves with the repository.
REPO_ROOT = _override('DIRECTOR_REPO_ROOT', PROJECT_ROOT.parents[2])
SKILLS_ROOT = REPO_ROOT / '.agents'

# storage_layout.py is the one published file still living outside the checkout:
# it is borrowed from the host's legacy production directory, which is not part of
# this repository.  It stays put until that file is brought into the checkout.
PRODUCTION_ROOT = WORKSPACE_ROOT / 'production'
STORAGE_LAYOUT = PRODUCTION_ROOT / 'storage_layout.py'
