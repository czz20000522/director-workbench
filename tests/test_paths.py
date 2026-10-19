"""Path provenance: one module owns every root, and no source re-inlines one.

The expectations here are frozen copies of what the inlined expressions resolved to
before ``paths.py`` existed, so a rename, a re-derivation at the wrong depth, or a
stale environment override fails loudly instead of silently pointing elsewhere.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

import paths

HOST_TAIL = 'Comfy' + '-Desktop'
ABSOLUTE_HOST_PATH = re.compile(r'[A-Za-z]:[\\/][^\'"\s]*' + HOST_TAIL)
DEPTH_DERIVED_ROOT = re.compile(r'parents\[[2-9]\]')
SOURCE_DIRECTORIES = ('backend', 'tools', 'mcp_server')

# The frozen expectations name this machine's default D: roots.
pytestmark = [pytest.mark.windows, pytest.mark.host_tool]


def test_machine_roots_are_the_recorded_host_paths():
    assert paths.COMFY_ROOT == Path('D:/Comfy-Desktop')
    assert paths.SHARED_ROOT == Path('D:/Comfy-Desktop/ComfyUI-Shared')
    data = Path('D:/software/JetBrains/python_workspace/director-workbench-data')
    assert paths.DATA_ROOT == data
    assert paths.RUNTIME_ROOT == data / 'runtime'
    assert paths.CONFIG_ROOT == data / '.config/director-workbench'
    assert paths.INSTALL_ROOT == Path('D:/Comfy-Desktop/ComfyUI-Installs/第一个comfyui配置/ComfyUI')
    assert paths.COMFY_PYTHON == Path(
        'D:/Comfy-Desktop/ComfyUI-Installs/第一个comfyui配置/ComfyUI/.venv/Scripts/python.exe')
    assert paths.LLAMA_ROOT == Path('D:/software/llama.cpp')


def test_repository_roots_follow_this_checkout():
    assert paths.PROJECT_ROOT == Path(__file__).resolve().parents[1]
    assert paths.REPO_ROOT == paths.PROJECT_ROOT
    assert paths.SKILLS_ROOT == paths.PROJECT_ROOT / '.agents'
    assert (paths.SKILLS_ROOT / 'skills/aigc-video-production/scripts/h3_render.py').is_file()


def test_files_borrowed_from_outside_the_checkout_now_travel_with_it():
    assert paths.STORAGE_LAYOUT == paths.PROJECT_ROOT / 'production/storage_layout.py'
    assert paths.STORAGE_LAYOUT.is_file()


def test_machine_roots_are_not_derived_from_the_checkout_location():
    for root in (paths.COMFY_ROOT, paths.SHARED_ROOT, paths.DATA_ROOT,
                 paths.RUNTIME_ROOT, paths.CONFIG_ROOT, paths.INSTALL_ROOT, paths.LLAMA_ROOT):
        assert root.is_absolute()
        assert not root.is_relative_to(paths.PROJECT_ROOT)


def test_a_foreign_host_can_supply_its_own_roots():
    environment = {**os.environ, 'DIRECTOR_COMFY_ROOT': 'X:/comfy', 'DIRECTOR_DATA_ROOT': 'X:/data'}
    probe = subprocess.run([sys.executable, '-c', 'import paths; print(paths.SHARED_ROOT); print(paths.DATA_ROOT)'],
                           cwd=str(paths.PROJECT_ROOT), env=environment,
                           capture_output=True, text=True, check=True)
    assert [Path(line) for line in probe.stdout.splitlines()] == [
        Path('X:/comfy/ComfyUI-Shared'), Path('X:/data')]


def test_no_source_inlines_a_host_path_or_derives_one_by_depth():
    tracked = subprocess.run(['git', 'ls-files', '*.py'], cwd=str(paths.PROJECT_ROOT),
                             capture_output=True, text=True, check=True).stdout.splitlines()
    offenders = []
    for relative in tracked:
        if relative == 'paths.py' or relative.startswith('tests/'):
            continue
        if '/' in relative and not relative.startswith(SOURCE_DIRECTORIES):
            continue
        source = (paths.PROJECT_ROOT / relative).read_text(encoding='utf-8')
        for number, line in enumerate(source.splitlines(), start=1):
            if ABSOLUTE_HOST_PATH.search(line) or DEPTH_DERIVED_ROOT.search(line):
                offenders.append(f'{relative}:{number}: {line.strip()}')
    assert offenders == [], 'Import from paths.py instead:\n' + '\n'.join(offenders)
