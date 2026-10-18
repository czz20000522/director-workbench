"""Critical project JSON is written atomically: a failed replacement keeps the old document."""
import json

import pytest

from backend import app as backend
from test_private_workbench import create, private_workbench  # noqa: F401  (pytest fixture)


def failing_replace(*args, **kwargs):
    raise PermissionError('locked for the test')


def test_write_json_atomic_keeps_the_previous_document_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / 'catalog.json'
    path.write_text('{"projects": ["old"]}\n', encoding='utf-8')
    monkeypatch.setattr(backend, 'replace_prepared', failing_replace)
    with pytest.raises(PermissionError):
        backend.write_json_atomic(path, {'projects': ['new']})
    assert path.read_text(encoding='utf-8') == '{"projects": ["old"]}\n'
    assert list(tmp_path.glob('.*.tmp')) == []


def test_write_json_atomic_writes_readable_json(tmp_path):
    path = tmp_path / 'manifest.json'
    backend.write_json_atomic(path, {'id': 'demo', 'title': '作品'})
    assert json.loads(path.read_text(encoding='utf-8')) == {'id': 'demo', 'title': '作品'}


def test_failed_manifest_save_keeps_the_previous_manifest(monkeypatch, private_workbench):
    a, _, _, _ = private_workbench
    project = create(a)
    manifest_path = backend.PROJECTS_ROOT / f"{project['id']}.json"
    before = manifest_path.read_text(encoding='utf-8')
    manifest = json.loads(before)
    manifest['title'] = '改过的标题'
    monkeypatch.setattr(backend, 'replace_prepared', failing_replace)
    with pytest.raises(PermissionError):
        backend.save_project_manifest(manifest)
    assert manifest_path.read_text(encoding='utf-8') == before
    assert list(manifest_path.parent.glob('.*.tmp')) == []
