import asyncio

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


def test_invalid_credentials_and_no_cookie_fallback(private_client):
    client, _, _ = private_client
    assert login(client, password='wrong').status_code == 401
    assert login(client, username='user999').status_code == 401
    assert client.get('/api/probe', headers={'x-workspace-user': 'user001'}).status_code == 401
    assert login(client).status_code == 200
    assert client.get('/api/probe', headers={'authorization': 'Bearer invalid'}).status_code == 401


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
