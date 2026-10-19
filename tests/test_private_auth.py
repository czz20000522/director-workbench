import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from backend.private_auth import COOKIE, PrivateAuth, current_principal
from backend.private_sessions import AccountStore, SessionManager


@pytest.fixture
def private_client(tmp_path):
    accounts = AccountStore(tmp_path / 'accounts.json', permitted_root=tmp_path)
    accounts.create_account('user001', 'test-one')
    accounts.create_account('user002', 'test-two')
    sessions = SessionManager(accounts)
    app = FastAPI()

    @app.api_route('/api/probe', methods=['GET', 'POST'])
    def probe():
        return {'username': current_principal.get().identity.username}

    @app.get('/media-file')
    def media():
        return {'private': True}

    wrapped = PrivateAuth(app, sessions=sessions, origins=('http://testserver',),
                          authorize=lambda request, principal: None)
    return TestClient(wrapped), sessions, accounts


def login(client, username='user001', password='test-one'):
    return client.post('/api/auth/login', json={'username': username, 'password': password},
                       headers={'origin': 'http://testserver'})


def test_cookie_login_context_and_logout(private_client):
    client, sessions, _ = private_client
    for path in ('/api/probe', '/media-file', '/openapi.json'):
        assert client.get(path).status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert 'token' not in response.json()
    token = client.cookies.get(COOKIE)
    assert client.get('/api/probe').json() == {'username': 'user001'}
    assert current_principal.get() is None
    assert client.post('/api/probe', headers={'origin': 'http://testserver', 'x-workspace-user': 'user001'}).status_code == 200
    response = client.post('/api/auth/logout', headers={'origin': 'http://testserver', 'x-workspace-user': 'user001'})
    assert response.status_code == 200
    assert sessions.lookup(token) is None
    assert client.get('/api/probe').status_code == 401


def test_agent_token_login_returns_bearer_without_cookie(private_client):
    client, sessions, _ = private_client
    response = client.post('/api/auth/agent-token',
                           json={'username': 'user001', 'password': 'test-one'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['username'] == 'user001'
    assert payload['token']
    assert 'set-cookie' not in response.headers
    assert client.cookies.get(COOKIE) is None
    assert client.get('/api/probe', headers={'authorization': 'Bearer ' + payload['token']}).json() == {'username': 'user001'}
    assert client.post('/api/auth/agent-token', json={'username': 'user001', 'password': 'wrong'}).status_code == 401


def test_invalid_credentials_and_no_cookie_fallback(private_client):
    client, _, _ = private_client
    assert login(client, password='wrong').status_code == 401
    assert login(client, username='user999').status_code == 401
    assert client.get('/api/probe', headers={'x-workspace-user': 'user001'}).status_code == 401
    assert login(client).status_code == 200
    assert client.get('/api/probe', headers={'authorization': 'Bearer invalid'}).status_code == 401


def test_http_login_failure_reservation_counts_once_and_success_clears(private_client, monkeypatch):
    client, _, _ = private_client
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 5)

    for _ in range(2):
        assert login(client, password='wrong').status_code == 401
    assert login(client).status_code == 200

    # A successful login clears the two reservations, so a fresh five-failure
    # window is available for the same account/source.
    for _ in range(5):
        assert login(client, password='wrong').status_code == 401
    assert login(client, password='wrong').status_code == 429


def test_http_login_failure_limit_survives_auth_restart(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 5)
    first = _persistent_auth(tmp_path)
    first_client = TestClient(first)
    for _ in range(5):
        assert login(first_client, password='wrong').status_code == 401

    restarted_client = TestClient(_persistent_auth(tmp_path))
    assert login(restarted_client, password='wrong').status_code == 429


def test_cross_origin_missing_tab_and_stale_tab(private_client):
    client, _, _ = private_client
    assert client.post('/api/auth/login', json={'username': 'user001', 'password': 'test-one'}, headers={'origin': 'http://evil.test'}).status_code == 403
    login(client)
    assert client.post('/api/probe', headers={'origin': 'http://testserver'}).status_code == 401
    assert client.post('/api/probe', headers={'x-workspace-user': 'user001'}).status_code == 403
    assert client.get('/api/probe', headers={'origin': 'http://evil.test'}).status_code == 403
    first = client.cookies.get(COOKIE)
    login(client, 'user002', 'test-two')
    assert client.get('/api/probe', headers={'x-workspace-user': 'user001'}).status_code == 401
    assert client.get('/api/probe', headers={'authorization': 'Bearer ' + first}).status_code == 401


def test_allowed_origin_preflight_is_answered_before_auth(private_client):
    client, _, _ = private_client
    response = client.options('/api/projects/demo/upload', headers={
        'Origin': 'http://testserver',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type,x-workspace-user',
    })
    assert response.status_code == 204
    assert response.headers['access-control-allow-origin'] == 'http://testserver'
    assert response.headers['access-control-allow-credentials'] == 'true'
    assert 'x-workspace-user' in response.headers['access-control-allow-headers']


def test_auth_bootstrap_responses_expose_allowed_origin(private_client):
    client, _, _ = private_client
    config = client.get('/api/auth/config', headers={'Origin': 'http://testserver'})
    assert config.status_code == 200
    assert config.headers['access-control-allow-origin'] == 'http://testserver'
    login_response = login(client)
    assert login_response.headers['access-control-allow-origin'] == 'http://testserver'
    me = client.get('/api/auth/me', headers={'Origin': 'http://testserver'})
    assert me.status_code == 200
    assert me.headers['access-control-allow-origin'] == 'http://testserver'


def test_disallowed_origin_preflight_is_rejected(private_client):
    client, _, _ = private_client
    response = client.options('/api/projects/demo/upload', headers={
        'Origin': 'http://evil.test',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type',
    })
    assert response.status_code == 403


def test_bearer_and_password_revocation(private_client):
    client, sessions, accounts = private_client
    token = sessions.login('user002', 'test-two')
    headers = {'authorization': 'Bearer ' + token}
    assert client.post('/api/probe', headers=headers).json() == {'username': 'user002'}
    accounts.set_password('user002', 'changed')
    assert client.get('/api/probe', headers=headers).status_code == 401


def test_authorization_callback_is_mandatory_and_precedes_app(private_client):
    _, sessions, _ = private_client
    async def forbidden_app(scope, receive, send):
        pytest.fail('Unauthorized request reached application')
    client = TestClient(PrivateAuth(forbidden_app, sessions=sessions, origins=('http://testserver',),
                                    authorize=lambda request, principal: (404, '作品不存在')))
    token = sessions.login('user001', 'test-one')
    assert client.get('/api/probe', headers={'authorization': 'Bearer ' + token}).status_code == 404


def test_stream_stops_after_revocation(private_client):
    _, sessions, _ = private_client
    token = sessions.login('user001', 'test-one')
    messages = []
    async def streaming(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
        await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
        sessions.logout(token)
        await send({'type': 'http.response.body', 'body': b'private-after-logout', 'more_body': True})
        pytest.fail('Revoked stream kept running')
    app = PrivateAuth(streaming, sessions=sessions, origins=('http://testserver',), authorize=lambda request, principal: None)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    async def send(message):
        messages.append(message)
    asyncio.run(app({'type': 'http', 'method': 'GET', 'path': '/api/events', 'query_string': b'', 'headers': [(b'authorization', ('Bearer ' + token).encode())], 'server': ('testserver', 80), 'scheme': 'http'}, receive, send))
    assert [m['body'] for m in messages if m['type'] == 'http.response.body'] == [b'first', b'']
    assert messages[-1]['more_body'] is False
    assert current_principal.get() is None


def test_starlette_stream_generator_closes_on_session_revocation(private_client):
    _, sessions, _ = private_client
    token = sessions.login('user001', 'test-one')
    closed = []
    app = FastAPI()

    @app.get('/api/events')
    async def events():
        async def generate():
            try:
                yield b'data: first\n\n'
                sessions.logout(token)
                yield b'data: private-after-logout\n\n'
                pytest.fail('Stream continued after revocation')
            finally:
                closed.append(True)
        return StreamingResponse(generate(), media_type='text/event-stream')

    client = TestClient(PrivateAuth(app, sessions=sessions, origins=('http://testserver',), authorize=lambda request, principal: None))
    response = client.get('/api/events', headers={'Authorization': 'Bearer ' + token})
    assert response.content == b'data: first\n\n'
    assert closed == [True]


def _persistent_auth(tmp_path):
    accounts = AccountStore(tmp_path / 'accounts.json', permitted_root=tmp_path)
    if 'user001' not in accounts._read()['users']:
        accounts.create_account('user001', 'test-one')
    sessions = SessionManager(accounts)
    return PrivateAuth(FastAPI(), sessions=sessions, origins=('http://testserver',),
                       authorize=lambda request, principal: None,
                       database_path=tmp_path / 'tasks.sqlite3')


def test_login_failures_survive_auth_restart_and_isolate_sources(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 2)
    first = _persistent_auth(tmp_path)
    first.record_login_failure('user001', '10.0.0.1')
    first.record_login_failure('user001', '10.0.0.1')

    restarted = _persistent_auth(tmp_path)
    assert not restarted.login_allowed('user001', '10.0.0.1')
    assert restarted.login_allowed('user001', '10.0.0.2')
    with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
        assert connection.execute(
            'SELECT identity, source, COUNT(*) FROM login_failure_events GROUP BY identity, source'
        ).fetchall() == [('user001', '10.0.0.1', 2), ('user001', '10.0.0.2', 1)]


def test_login_failure_expiry_and_concurrent_recording_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 5)
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_WINDOW_SECONDS', 60)
    clock = {'now': 1000.0}
    monkeypatch.setattr('backend.private_auth.time', type('Clock', (), {
        'time': staticmethod(lambda: clock['now']),
        'monotonic': staticmethod(lambda: clock['now']),
    }))
    auth = _persistent_auth(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: auth.record_login_failure('user001', '10.0.0.1'), range(32)))
    assert not auth.login_allowed('user001', '10.0.0.1')
    clock['now'] += 61
    assert auth.login_allowed('user001', '10.0.0.1')
    with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
        assert connection.execute('SELECT COUNT(*) FROM login_failure_events').fetchone()[0] == 1


def test_login_reservation_is_atomic_across_auth_instances(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 5)
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_WINDOW_SECONDS', 60)
    auths = [_persistent_auth(tmp_path) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=len(auths)) as pool:
        allowed = list(pool.map(lambda auth: auth.login_allowed('user001', '10.0.0.1'), auths))

    assert sum(allowed) == 5
    assert all(not auth.login_allowed('user001', '10.0.0.1') for auth in auths)
    with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM login_failure_events WHERE identity=? AND source=?',
            ('user001', '10.0.0.1'),
        ).fetchone()[0] == 5


def test_failure_confirmation_does_not_duplicate_reserved_token(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.private_auth.LOGIN_FAILURE_LIMIT', 5)
    auth = _persistent_auth(tmp_path)
    assert auth.login_allowed('user001', '10.0.0.1')
    auth.record_login_failure('user001', '10.0.0.1')
    with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM login_failure_events WHERE identity=? AND source=?',
            ('user001', '10.0.0.1'),
        ).fetchone()[0] == 1


def test_login_failure_logs_do_not_include_password(tmp_path, caplog):
    auth = _persistent_auth(tmp_path)
    caplog.set_level(logging.WARNING)
    auth.database_path = tmp_path / 'missing' / 'read-only.sqlite3'
    auth.record_login_failure('user001', '10.0.0.1')
    assert 'test-one' not in caplog.text
