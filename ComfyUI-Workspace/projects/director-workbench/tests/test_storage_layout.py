"""Migration contracts: only listed legacy files move into an owned asset root."""
import json
from pathlib import Path

import pytest

from backend.storage import storage_roots, relocate_asset
from backend.user_context import Layout, UserContext, project_location


def test_configured_roots_and_legacy_reference_are_distinct(tmp_path):
    root = tmp_path / 'Comfy-Desktop'
    config = root / 'ComfyUI-Workspace/config/storage.json'
    config.parent.mkdir(parents=True)
    archive = tmp_path / 'DirectorWorkspaces/user001/series/Example/Work/input'
    old = root / 'ComfyUI-Shared/input/Example/Work'
    config.write_text(json.dumps({'schema_version': 1, 'roots': {
        'input': str(tmp_path / 'Comfy-Assets/input'),
        'output': str(tmp_path / 'Comfy-Assets/output'),
        'workspaces': str(tmp_path / 'DirectorWorkspaces/user001/series'),
        'private': str(tmp_path / 'DirectorWorkspaces'),
    }, 'legacy_assets': [{'source': str(old), 'destination': str(archive)}]}), encoding='utf-8')
    roots = storage_roots(root)
    assert roots['input'] == (tmp_path / 'Comfy-Assets/input').resolve()
    assert relocate_asset(old / 'shot.png', root, roots) == archive / 'shot.png'
    assert relocate_asset(root / 'ComfyUI-Shared/input/new.png', root, roots) == roots['input'] / 'new.png'
    assert relocate_asset(root / 'ComfyUI-Shared/models/model.bin', root, roots) == root / 'ComfyUI-Shared/models/model.bin'
    with pytest.raises(ValueError):
        relocate_asset(old / '..' / 'other.png', root, roots)


def test_registered_series_work_location_stays_in_owner_tree(tmp_path):
    layout = Layout(tmp_path / 'users', (tmp_path,))
    project_id = 'p-' + 'a' * 32
    location = project_location('噜噜系列', '你们上班我连休', project_id)
    layout.root.mkdir()
    (layout.root / 'ownership.json').write_text(json.dumps({
        'schema_version': 1, 'projects': {project_id: 'user001'},
        'selected': {}, 'locations': {project_id: location.as_posix()},
    }), encoding='utf-8')
    a = UserContext('user001', layout)
    b = UserContext('user002', layout)
    assert a.project(project_id) == a.root / location
    assert a.task(project_id, 't-1').parent == a.project(project_id) / 'tasks'
    with pytest.raises(ValueError):
        b.project(project_id)
