"""RealESRGAN detail pass for large keyframe exports.

The workbench process has no torch, so the only route to the GPU is a small
graph submitted to the ComfyUI instance that just rendered the frame. Each pass
doubles the long edge; the caller then reuses the existing exact-size Lanczos
crop, because a super-resolution model cannot change the aspect ratio or land on
an arbitrary pixel size.

Never fatal: an offline engine, a missing model, a rejected graph or a timeout
all return ``None`` so the caller keeps the native render and records a plain
Lanczos receipt. Post-processing must not destroy a generation that already
succeeded.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .h3_duration import probe_video

MODEL = 'RealESRGAN_x2plus.pth'
# Two passes are 4x. A third pass on a 1056px render would build a 150 MP
# intermediate before it is thrown away by the final crop.
MAX_PASSES = 2
PASS_SECONDS = 240.0
POLL_SECONDS = 1.0


def engine_url() -> str:
    return os.environ.get('DIRECTOR_COMFY_URL', 'http://127.0.0.1:8188').rstrip('/')


def model_path(model_root: Path) -> Path:
    return Path(model_root) / 'upscale_models' / MODEL


def passes_for(native_long_edge: int, target_long_edge: int) -> int:
    """How many 2x passes are worth running before the final crop."""
    if native_long_edge <= 0 or target_long_edge <= native_long_edge:
        return 0
    needed = math.ceil(math.log(target_long_edge / native_long_edge, 2))
    return max(0, min(MAX_PASSES, needed))


def available(*, model_root: Path, url: str | None = None, timeout: float = 2.0) -> dict[str, Any]:
    """Cheap read-only probe; the caller may run this from a status endpoint."""
    model = model_path(model_root)
    result: dict[str, Any] = {'model': MODEL, 'model_present': model.is_file(), 'scale': 2,
                              'max_passes': MAX_PASSES, 'engine': False}
    try:
        _request('/system_stats', url=url, timeout=timeout)
        result['engine'] = True
    except (OSError, ValueError):
        pass
    result['available'] = bool(result['model_present'] and result['engine'])
    return result


def _request(path: str, payload: dict[str, Any] | None = None, *, url: str | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(
        (url or engine_url()) + path, data=data,
        headers={'Content-Type': 'application/json'} if data else {},
        method='POST' if data else 'GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode('utf-8')) if body else {}


def _graph(name: str, prefix: str) -> dict[str, Any]:
    return {
        'load': {'class_type': 'LoadImage', 'inputs': {'image': name}},
        'model': {'class_type': 'UpscaleModelLoader', 'inputs': {'model_name': MODEL}},
        'upscale': {'class_type': 'ImageUpscaleWithModel',
                    'inputs': {'upscale_model': ['model', 0], 'image': ['load', 0]}},
        'save': {'class_type': 'SaveImage', 'inputs': {'images': ['upscale', 0], 'filename_prefix': prefix}},
    }


def _wait(prompt_id: str, *, url: str, stopped: Callable[[], bool], seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stopped():
            return {}
        try:
            entry = _request('/history/' + urllib.parse.quote(prompt_id, safe=''), url=url).get(prompt_id)
        except (OSError, ValueError):
            entry = None
        if entry:
            return entry
        time.sleep(POLL_SECONDS)
    return {}


def _saved_image(entry: dict[str, Any], output_root: Path) -> Path | None:
    for node in (entry.get('outputs') or {}).values():
        for image in (node or {}).get('images') or []:
            name = str(image.get('filename') or '')
            if name:
                candidate = Path(output_root) / str(image.get('subfolder') or '') / name
                if candidate.is_file():
                    return candidate
    return None


def upscale(source: Path, size: tuple[int, int], *, input_root: Path, output_root: Path,
            model_root: Path, stopped: Callable[[], bool] = lambda: False,
            url: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Return a sharper PNG plus a receipt, or ``None`` to keep the native file."""
    source = Path(source)
    if source.suffix.lower() != '.png' or not source.is_file():
        return None
    if not model_path(model_root).is_file():
        return None
    facts = probe_video(source)
    if not facts or not facts.get('width') or not facts.get('height'):
        return None
    target_edge = max(size)
    passes = passes_for(max(int(facts['width']), int(facts['height'])), target_edge)
    if passes == 0:
        return None
    staging = Path(input_root) / '_superres'
    produced = Path(output_root) / '_superres' / uuid.uuid4().hex
    temporaries: list[Path] = []
    current = source
    completed = 0
    try:
        staging.mkdir(parents=True, exist_ok=True)
        produced.mkdir(parents=True, exist_ok=True)
        for index in range(passes):
            if stopped():
                return None
            staged = staging / f'{uuid.uuid4().hex}.png'
            shutil.copy2(current, staged)
            temporaries.append(staged)
            # LoadImage resolves names relative to the ComfyUI input root, so the
            # staging subfolder has to stay in the name.
            reference = staged.relative_to(Path(input_root)).as_posix()
            prefix = f'_superres/{produced.name}/{index}'
            try:
                submitted = _request('/prompt', {'prompt': _graph(reference, prefix),
                                                 'client_id': f'director-workbench-superres-{uuid.uuid4().hex[:12]}'},
                                     url=url, timeout=60)
            except (OSError, ValueError):
                return None
            prompt_id = str(submitted.get('prompt_id') or '')
            if not prompt_id:
                return None
            entry = _wait(prompt_id, url=url or engine_url(), stopped=stopped, seconds=PASS_SECONDS)
            if not entry:
                return None
            saved = _saved_image(entry, Path(output_root))
            if saved is None:
                return None
            if current != source:
                temporaries.append(current)
            current = saved
            completed += 1
        after = probe_video(current)
        if not after or not after.get('width'):
            return None
        return current, {
            'method': 'ai_upscale',
            'model': MODEL,
            'passes': completed,
            'scale': 2 ** completed,
            'superseded_size': f"{facts['width']} × {facts['height']}",
            'upscaled_size': f"{after['width']} × {after['height']}",
            'temporary': [str(path) for path in temporaries],
        }
    except OSError:
        return None
    except Exception:  # noqa: BLE001 - a detail pass must not fail the task
        return None


def enhance(source: Path, size: tuple[int, int], *, input_root: Path, output_root: Path,
            model_root: Path, stopped: Callable[[], bool] = lambda: False) -> tuple[Path, dict[str, Any]] | None:
    """Adapter with the keyword shape :func:`output_resolution.resize` expects."""
    return upscale(source, size, input_root=input_root, output_root=output_root,
                   model_root=model_root, stopped=stopped)
