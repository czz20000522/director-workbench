"""Security hardening: browser headers, login throttling and status path redaction."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend import private_auth
from backend.private_auth import SecurityHeaders
from backend.routers import workbench_status


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

