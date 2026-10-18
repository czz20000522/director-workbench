"""Security hardening: browser headers, login throttling and status path redaction."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import app as backend
from backend import private_auth
from backend.private_auth import PrivateAuth, SecurityHeaders
from backend.routers import workbench_status
from backend.private_sessions import AccountStore, SessionManager


def test_security_headers_cover_the_page_and_the_api():
    application = FastAPI()

    @application.get('/page')
    def page():
        return {'ok': True}

    @application.get('/api/health')
    def health():
        return {'ok': True}

    application.add_middleware(SecurityHeaders)
    client = TestClient(application)
    for path in ('/page', '/api/health'):
        response = client.get(path)
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert response.headers['content-security-policy'].startswith("default-src 'self';")
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert response.headers['permissions-policy'] == 'camera=(), microphone=(), geolocation=(), usb=()'
        assert response.headers['cross-origin-opener-policy'] == 'same-origin'


def test_status_does_not_leak_the_machine_runtime_path(monkeypatch):
    monkeypatch.setattr(backend, 'comfy_status', lambda: {'online': False})
    payload = workbench_status.status()
    assert payload == {'comfy': {'online': False}}


def test_unhandled_exception_is_logged_and_redacted(caplog):
    application = FastAPI()
    application.add_exception_handler(Exception, backend.unhandled_exception)

    @application.get('/boom')
    def boom():
        raise RuntimeError('secret path C:\\private\\trace.py:99')

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get('/boom')
    assert response.status_code == 500
    assert response.json() == {'detail': backend.UNHANDLED_ERROR_DETAIL}
    assert 'secret path' in caplog.text
    assert 'trace.py' in caplog.text


def test_execution_script_paths_are_contained(tmp_path):
    allowed = tmp_path / 'tools'
    allowed.mkdir()
    safe = allowed / 'builder.py'
    safe.write_text('value = 1', encoding='utf-8')
    assert backend.validate_execution_script_path(safe, allowed_roots=(tmp_path,), label='构建器') == safe.resolve()
    with pytest.raises(ValueError, match='不在允许目录内'):
        backend.validate_execution_script_path(tmp_path.parent / 'outside.py', allowed_roots=(tmp_path,), label='构建器')


def test_repeated_login_failures_are_throttled(private_workbench, monkeypatch):
    a, _, _, _ = private_workbench
    monkeypatch.setattr(private_auth, 'LOGIN_FAILURE_LIMIT', 2)
    monkeypatch.setattr(private_auth, 'LOGIN_FAILURE_WINDOW_SECONDS', 60)
    client = TestClient(a.app)
    wrong = {'username': 'user001', 'password': 'wrong-password'}
    right = {'username': 'user001', 'password': 'one'}
    assert client.post('/api/auth/login', json=wrong).status_code == 401
    assert client.post('/api/auth/login', json=wrong).status_code == 401
    throttled = client.post('/api/auth/login', json=right)
    assert throttled.status_code == 429
    # Once the failure window has passed the same source may try again.
    monkeypatch.setattr(private_auth, 'LOGIN_FAILURE_WINDOW_SECONDS', 0)
    assert client.post('/api/auth/login', json=right).status_code == 200


def test_login_failure_keys_expire_and_are_bounded(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / 'accounts.json', permitted_root=tmp_path)
    store.create_account('user001', 'one')
    auth = PrivateAuth(FastAPI(), sessions=SessionManager(store), origins=('http://testserver',), authorize=lambda request, principal: None)
    monkeypatch.setattr(private_auth, 'LOGIN_FAILURE_MAX_KEYS', 2)
    monkeypatch.setattr(private_auth, 'LOGIN_FAILURE_WINDOW_SECONDS', 10)
    monkeypatch.setattr(private_auth.time, 'monotonic', lambda: 105.0)
    auth.login_failures = {('expired', 'source'): [90.0], ('fresh', 'source'): [100.0]}
    assert auth.login_allowed('fresh', 'source')
    assert ('expired', 'source') not in auth.login_failures
    for username in ('one', 'two', 'three'):
        auth.record_login_failure(username, 'source')
    assert len(auth.login_failures) == 2
    assert ('three', 'source') in auth.login_failures
