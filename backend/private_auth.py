"""Session transport for the existing ASGI application (one scheduler/process).

No credentials are inferred from a username, request host, or localhost access.
The caller installs a project authorization callback before enabling this layer.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import json
import threading
import time
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .private_sessions import AuthenticationError, SessionManager
from .user_context import SessionIdentity

COOKIE = 'director_session'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
# Failed logins are remembered per account and client source, so a slow PBKDF2 verifier
# cannot be turned into an online guessing loop.  Both values are module constants so a
# deployment or test can tighten them in one place.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 300


class SecurityHeaders:
    """Add browser-hardening headers to every response, including the static shell.

    Installed as the outermost middleware on the application: the authentication layer
    deliberately lets the unauthenticated page and its assets pass through untouched.
    """
    HEADERS = (
        (b'x-content-type-options', b'nosniff'),
        (b'x-frame-options', b'DENY'),
        (b'referrer-policy', b'no-referrer'),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in self.HEADERS if name not in present)
                message = {**message, 'headers': headers}
            await send(message)

        return await self.app(scope, receive, send_with_headers)


class _RevokedStream(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    identity: SessionIdentity
    token: str
    administrator: bool


current_principal: ContextVar[Principal | None] = ContextVar('director_principal', default=None)


class PrivateAuth:
    def __init__(self, app, *, sessions: SessionManager, origins: tuple[str, ...],
                 authorize: Callable, administrators: tuple[str, ...] = (), secure_cookie=False):
        if not origins or any(not origin.startswith(('http://', 'https://')) or origin.endswith('/') for origin in origins):
            raise ValueError('Configure exact browser origins without a trailing slash')
        self.app, self.sessions = app, sessions
        self.origins = frozenset(origins)
        self.authorize = authorize
        self.administrators = frozenset(administrators)
        self.secure_cookie = secure_cookie
        # (account, source) -> monotonic timestamps of recent failures, under a lock.
        self.login_failures: dict[tuple[str, str], list[float]] = {}
        self.login_lock = threading.Lock()

    def login_allowed(self, username: str, source: str) -> bool:
        key = (username.lower(), source or '')
        now = time.monotonic()
        with self.login_lock:
            attempts = [at for at in self.login_failures.get(key, ())
                        if now - at < LOGIN_FAILURE_WINDOW_SECONDS]
            if attempts:
                self.login_failures[key] = attempts
            else:
                self.login_failures.pop(key, None)
            return len(attempts) < LOGIN_FAILURE_LIMIT

    def record_login_failure(self, username: str, source: str) -> None:
        key = (username.lower(), source or '')
        with self.login_lock:
            self.login_failures.setdefault(key, []).append(time.monotonic())

    def clear_login_failures(self, username: str, source: str) -> None:
        with self.login_lock:
            self.login_failures.pop((username.lower(), source or ''), None)

    def authenticate(self, request):
        authorization = request.headers.get('authorization')
        bearer = authorization is not None
        if bearer:
            # An invalid explicit credential must never fall back to a cookie.
            token = authorization[7:] if authorization.startswith('Bearer ') else ''
        else:
            token = request.cookies.get(COOKIE, '')
        identity = self.sessions.lookup(token)
        return (Principal(identity, token, identity.username in self.administrators) if identity else None), bearer

    @staticmethod
    def public_identity(principal):
        return {'username': principal.identity.username, 'administrator': principal.administrator,
                'expires_at': principal.identity.expires_at}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path, method = request.url.path, request.method
        protected = path.startswith(('/api/', '/mcp/', '/media/', '/media-file', '/material-file', '/workflow-file')) or path in {'/mcp', '/openapi.json', '/docs', '/redoc'}
        if not protected:
            return await self.app(scope, receive, send)

        def response_headers(extra=None):
            headers = dict(extra or {})
            headers.setdefault('Cache-Control', 'no-store')
            if origin is not None and origin in self.origins:
                headers.update({
                    'Access-Control-Allow-Origin': origin,
                    'Access-Control-Allow-Credentials': 'true',
                    'Vary': 'Origin',
                })
            return headers

        async def respond(status, detail):
            headers = response_headers()
            await JSONResponse({'detail': detail}, status_code=status, headers=headers)(scope, receive, send)

        origin = request.headers.get('origin')
        if origin is not None and origin not in self.origins:
            return await respond(403, '请求来源未获允许')
        # A browser sends this unauthenticated preflight when the UI is
        # reached through a dev proxy, reverse proxy, or another approved
        # origin and the session binding header is present.  It must be
        # answered before cookie authentication; otherwise the browser only
        # reports a generic "Failed to fetch" and never sends the upload.
        if method == 'OPTIONS' and origin is not None:
            requested_headers = request.headers.get('access-control-request-headers', '')
            headers = {
                'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Allow-Methods': 'GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS',
                'Access-Control-Allow-Headers': requested_headers or 'Content-Type, X-Workspace-User, Authorization',
                'Access-Control-Max-Age': '600',
                'Cache-Control': 'no-store',
                'Vary': 'Origin, Access-Control-Request-Headers',
            }
            return await Response(status_code=204, headers=headers)(scope, receive, send)
        if path == '/api/auth/config' and method == 'GET':
            return await JSONResponse({'enabled': True}, headers=response_headers())(scope, receive, send)
        if path == '/api/auth/login' and method == 'POST':
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                return await respond(415, '登录请求需要 JSON')
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    return await respond(413, '登录请求过大')
            try:
                values = json.loads(body)
                if not isinstance(values, dict) or set(values) != {'username', 'password'}:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                return await respond(401, '用户名或密码错误')
            source = request.client.host if request.client is not None else ''
            username = values['username'] if isinstance(values['username'], str) else ''
            if not self.login_allowed(username, source):
                return await respond(429, '登录尝试过多，请稍后再试')
            try:
                token = self.sessions.login(values['username'], values['password'])
            except (ValueError, TypeError, UnicodeError, AuthenticationError):
                self.record_login_failure(username, source)
                return await respond(401, '用户名或密码错误')
            self.clear_login_failures(username, source)
            old_token = request.cookies.get(COOKIE, '')
            if old_token:
                self.sessions.logout(old_token)
            identity = self.sessions.lookup(token)
            principal = Principal(identity, token, identity.username in self.administrators)
            response = JSONResponse(self.public_identity(principal), headers=response_headers())
            response.set_cookie(COOKIE, token, httponly=True, secure=self.secure_cookie,
                                samesite='strict', path='/', max_age=int(self.sessions.ttl_seconds))
            return await response(scope, receive, send)

        principal, bearer = self.authenticate(request)
        if principal is None:
            return await respond(401, '请登录工作台')
        expected = request.headers.get('x-workspace-user')
        if expected is not None and expected != principal.identity.username:
            return await respond(401, '账号已切换，请刷新页面')
        if method not in SAFE_METHODS and not bearer:
            # Browser writes bind the tab identity as well as the shared cookie.
            if expected != principal.identity.username:
                return await respond(401, '请刷新页面确认当前账号')
            if origin is None:
                return await respond(403, '浏览器写入需要同源请求')
        if path == '/api/auth/me' and method == 'GET':
            return await JSONResponse(self.public_identity(principal), headers=response_headers())(scope, receive, send)
        if path == '/api/auth/logout' and method == 'POST':
            self.sessions.logout(principal.token)
            response = JSONResponse({'logged_out': True}, headers=response_headers())
            response.delete_cookie(COOKIE, path='/', httponly=True, secure=self.secure_cookie, samesite='strict')
            return await response(scope, receive, send)
        if path.startswith('/api/auth/'):
            return await respond(404, '接口不存在')

        context = current_principal.set(principal)
        ended = False
        try:
            decision = self.authorize(request, principal)
            if decision is not None:
                return await respond(*decision)

            async def guarded_send(message):
                nonlocal ended
                if ended:
                    return
                if message['type'] == 'http.response.start':
                    message = dict(message)
                    existing = [(k, v) for k, v in message.get('headers', []) if k.lower() not in {b'cache-control', b'access-control-allow-origin', b'access-control-allow-credentials', b'vary'}]
                    existing.append((b'cache-control', b'no-store'))
                    if origin is not None and origin in self.origins:
                        existing.extend([
                            (b'access-control-allow-origin', origin.encode()),
                            (b'access-control-allow-credentials', b'true'),
                            (b'vary', b'Origin'),
                        ])
                    message['headers'] = existing
                elif message['type'] == 'http.response.body' and self.sessions.lookup(principal.token) is None:
                    # Covers long-lived SSE and streaming downloads after logout.
                    ended = True
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                    raise _RevokedStream()
                await send(message)

            try:
                await self.app(scope, receive, guarded_send)
            except _RevokedStream:
                pass
        finally:
            current_principal.reset(context)
