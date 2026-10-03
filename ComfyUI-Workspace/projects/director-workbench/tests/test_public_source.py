"""Observable public export boundaries and an actual empty-checkout import."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import export_public_source as publish


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def source_repo(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / '.gitignore').write_text('/*\n!/.gitignore\n!/backend/\n/backend/*\n!/backend/app.py\n', encoding='utf-8')
    (tmp_path / 'backend').mkdir()
    (tmp_path / 'backend/app.py').write_text('print("public source")\n', encoding='utf-8')
    git(tmp_path, 'add', '.')
    return tmp_path


def test_export_never_sweeps_untracked_private_files(source_repo, tmp_path):
    (source_repo / 'backend/private.json').write_text('{"api_key":"private-value"}', encoding='utf-8')
    destination = tmp_path / 'new-export'
    sources = publish.export(destination, source_repo)
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    assert actual == {p.relative_to(source_repo).as_posix() for p in sources} == {'.gitignore', 'backend/app.py'}
    assert not (destination / '.git').exists()


def test_forced_add_cannot_bypass_allowlist(source_repo):
    (source_repo / 'backend/private.json').write_text('{}', encoding='utf-8')
    git(source_repo, 'add', '-f', 'backend/private.json')
    with pytest.raises(ValueError, match='backend/private.json'):
        publish.public_sources(source_repo)


def test_private_runtime_is_denied_even_if_allowlisted(source_repo):
    with (source_repo / '.gitignore').open('a', encoding='utf-8') as handle:
        handle.write('!/runtime/\n!/runtime/account.json\n')
    (source_repo / 'runtime').mkdir()
    (source_repo / 'runtime/account.json').write_text('{}', encoding='utf-8')
    git(source_repo, 'add', '.')
    with pytest.raises(ValueError, match='runtime/account.json'):
        publish.public_sources(source_repo)


def test_sensitive_values_are_reported_only_by_path(source_repo):
    secret = 'sk-' + 'synthetic01234567890123456789'
    (source_repo / 'backend/app.py').write_text(f'key = {secret!r}\n', encoding='utf-8')
    with pytest.raises(ValueError) as error:
        publish.public_sources(source_repo)
    assert 'backend/app.py' in str(error.value) and secret not in str(error.value)


def test_export_rejects_existing_destination(source_repo, tmp_path):
    destination = tmp_path / 'kept'
    destination.mkdir()
    marker = destination / 'important.txt'
    marker.write_text('keep', encoding='utf-8')
    with pytest.raises(ValueError, match='Destination must be new'):
        publish.export(destination, source_repo)
    assert marker.read_text(encoding='utf-8') == 'keep'


def test_ancestor_link_is_rejected(source_repo, tmp_path):
    alias = tmp_path / 'alias'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(source_repo / 'backend')], check=True, capture_output=True)
    else:
        alias.symlink_to(source_repo / 'backend', target_is_directory=True)
    with pytest.raises(ValueError, match='Linked'):
        publish.no_links(alias / 'app.py')


def test_clean_public_checkout_starts_private_app_without_user_works(tmp_path):
    root = publish.ROOT
    checkout = tmp_path / 'checkout'
    publish.export(checkout, root)
    project = checkout / 'ComfyUI-Workspace/projects/director-workbench'
    code = '''
from pathlib import Path
from fastapi.testclient import TestClient
from backend.private_sessions import AccountStore
AccountStore(Path('runtime/accounts.json').resolve()).create_account('user001', 'synthetic-password')
from backend import private_app
from backend import app as backend
assert backend.load_project_catalog() == {'projects': []}
assert backend.CURRENT_PROJECT_ID == ''
assert not backend.PROJECT_CATALOG_PATH.exists()
assert not backend.PLAN_PATH.exists()
client = TestClient(private_app.app)
assert client.get('/.well-known/director-workbench.json').status_code == 200
assert client.post('/api/auth/login', json={'username':'user001','password':'synthetic-password'}).status_code == 200
response = client.get('/api/projects')
assert response.status_code == 200, response.text
assert response.json()['projects'] == [], response.text
print('empty private checkout passed')
'''
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('DIRECTOR_PROJECT_ID', None)
    environment.update(DIRECTOR_ACCOUNTS_FILE=str(project / 'runtime/accounts.json'),
                       DIRECTOR_ADMIN_USERS='user001', DIRECTOR_PRIVATE_ROOT=str(checkout / 'ComfyUI-Workspace/private-workspaces'))
    result = subprocess.run([sys.executable, '-c', code], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'empty private checkout passed' in result.stdout


def test_catalog_without_global_default_remains_unselected_after_private_work_creation(tmp_path, monkeypatch):
    from backend import app as backend
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'projects': [{'id': 'private-work', 'manifest': 'private.json'}]}), encoding='utf-8')
    monkeypatch.setattr(backend, 'PROJECT_CATALOG_PATH', catalog)
    monkeypatch.delenv('DIRECTOR_PROJECT_ID', raising=False)
    assert backend.initial_project_manifest()['id'] == ''
    catalog.write_text(json.dumps({'default_project_id': 'missing', 'projects': []}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='项目不存在'):
        backend.initial_project_manifest()
    catalog.write_text('broken', encoding='utf-8')
    with pytest.raises(RuntimeError, match='无法读取'):
        backend.initial_project_manifest()
