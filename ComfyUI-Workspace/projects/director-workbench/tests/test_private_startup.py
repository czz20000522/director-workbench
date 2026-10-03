import runpy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend.private_sessions import AccountStore
from tools.private_accounts import main


def test_account_command_and_authenticated_entry(tmp_path, monkeypatch):
    path = tmp_path / 'accounts.json'
    values = iter(('synthetic-password', 'synthetic-password'))
    main(['create', 'user001', '--accounts-file', str(path)], password_prompt=lambda _: next(values))
    assert 'synthetic-password' not in path.read_text()
    monkeypatch.setattr(backend, 'PRIVATE_WORKSPACES', None)
    app = FastAPI()
    app.router.routes = list(backend.app.router.routes)
    monkeypatch.setattr(backend, 'app', app)
    monkeypatch.setenv('DIRECTOR_ACCOUNTS_FILE', str(path))
    monkeypatch.setenv('DIRECTOR_PRIVATE_ROOT', str(tmp_path / 'users'))
    monkeypatch.setenv('DIRECTOR_ADMIN_USERS', 'user001')
    monkeypatch.setenv('DIRECTOR_PRIVATE_ORIGINS', 'http://testserver')
    namespace = runpy.run_module('backend.private_app')
    client = TestClient(namespace['app'])
    assert client.get('/api/auth/config').json() == {'enabled': True}
    assert client.get('/api/projects').status_code == 401
    response = client.post('/api/auth/login', json={'username': 'user001', 'password': 'synthetic-password'})
    assert response.status_code == 200
    assert response.json()['administrator'] is True


def test_missing_accounts_do_not_start_anonymous_service(tmp_path, monkeypatch):
    monkeypatch.setenv('DIRECTOR_ACCOUNTS_FILE', str(tmp_path / 'missing.json'))
    with pytest.raises(RuntimeError, match='创建账号'):
        runpy.run_module('backend.private_app')
