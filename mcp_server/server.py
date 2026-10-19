"""Local stdio adapter. All business state remains in the HTTP workbench."""
from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

if __package__:
    from .models import ScriptScene, ShotChanges, ShotMaterials
    from . import errors, media, tools_creative, tools_generation, tools_media, tools_reference
else:
    from models import ScriptScene, ShotChanges, ShotMaterials
    import errors, media, tools_creative, tools_generation, tools_media, tools_reference


class _Client:
    def __init__(self, request: Callable[..., Any], project_path: Callable[[str], str],
                 path_id: Callable[[str, str], str], fetch: Callable[..., Any], remote: bool):
        self.request = request
        self.project_path = project_path
        self.path_id = path_id
        self.fetch = fetch
        self.remote = remote


def create_server(base_url: str, transport: httpx.AsyncBaseTransport | None = None, *, session_token: str | None = None,
                  session_token_provider: Callable[[], str | None] | None = None,
                  transport_provider: Callable[[], httpx.AsyncBaseTransport] | None = None,
                  remote: bool = False) -> MCPServer:
    parsed = urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('首版 MCP 仅连接本机 HTTP 工作台地址')
    base_url = base_url.rstrip('/')
    if session_token is not None and (not session_token or any(char.isspace() for char in session_token)):
        raise ValueError('无效的工作台会话凭据')
    session_headers = {'Authorization': 'Bearer ' + session_token} if session_token else {}
    server = MCPServer('director-workbench')
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    write_record = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

    def current_headers() -> dict[str, str] | None:
        if session_token_provider is None:
            return session_headers
        token = session_token_provider()
        if not token:
            return None
        return {'Authorization': 'Bearer ' + token}

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = current_headers()
        if headers is None:
            return errors.error('authentication_required', '请重新登录本人工作台账号',
                                kind='transport', http_status=401)
        try:
            async with httpx.AsyncClient(base_url=base_url, transport=transport_provider() if transport_provider else transport, timeout=30, trust_env=False, follow_redirects=False, headers=headers) as client:
                response = await client.request(method, path, **kwargs)
            data = response.json()
        except httpx.RequestError:
            if method == 'GET':
                return errors.error('workbench_unavailable', '工作台请求未取得回执，请稍后重试',
                                    kind='transport', retryable=True)
            return errors.error('workbench_unavailable',
                                '工作台请求未取得回执；写入结果未知，请先读取最新记录核对，不要直接重发',
                                kind='outcome_unknown', outcome_unknown=True)
        except ValueError:
            return errors.error('invalid_response', '工作台未返回 JSON', kind='transport')
        if not response.is_success:
            payload = errors.http_error(response.status_code, data)
            if remote:
                payload['error'] = media.redact_paths(payload['error'])
            return payload
        return {'ok': True, 'data': media.redact_paths(data) if remote else data}

    async def fetch(path: str) -> dict[str, Any]:
        """Fetch an already-authorized media URL as bytes (bounded inline reads)."""
        headers = current_headers()
        if headers is None:
            return errors.error('authentication_required', '请重新登录本人工作台账号',
                                kind='transport', http_status=401)
        try:
            async with httpx.AsyncClient(base_url=base_url, transport=transport_provider() if transport_provider else transport, timeout=30, trust_env=False, follow_redirects=False, headers=headers) as client:
                response = await client.get(path)
        except httpx.RequestError:
            return errors.error('workbench_unavailable', '媒体请求未取得回执，请稍后重试',
                                kind='transport', retryable=True)
        if response.status_code == 401:
            return errors.error('authentication_required', '媒体链接需要本人会话',
                                kind='transport', http_status=401)
        if response.status_code == 410:
            return errors.error('media_ref_expired', '媒体链接已过期，请重新读取媒体列表',
                                kind='business', http_status=410)
        if not response.is_success:
            return errors.error('media_unavailable', '媒体文件不可读取', kind='business',
                                http_status=response.status_code, detail=response.text[:2000])
        return {'ok': True, 'data': {'content': response.content,
                                     'content_type': response.headers.get('content-type', '')}}

    def project_path(project_id: str) -> str:
        if not project_id or project_id in {'.', '..'} or '/' in project_id or '\\' in project_id:
            raise errors.ToolInputError('invalid_project_id', '必须提供有效的作品 ID')
        return '/api/projects/' + quote(project_id, safe='')

    def path_id(value: str, label: str) -> str:
        if not value or value in {'.', '..'} or '/' in value or chr(92) in value or chr(0) in value:
            raise errors.ToolInputError('invalid_identifier', f'必须提供有效{label} ID')
        return quote(value, safe='')

    client = _Client(request, project_path, path_id, fetch, remote)
    guarded = errors.ToolServer(server)
    tools_creative.register(guarded, client, read_only=read_only, write_record=write_record)
    tools_reference.register(guarded, client, read_only=read_only, write_record=write_record)
    tools_generation.register(guarded, client, read_only=read_only, write_record=write_record)
    tools_media.register(guarded, client, read_only=read_only, write_record=write_record)
    return server


if __name__ == '__main__':
    create_server(os.environ.get('DIRECTOR_WORKBENCH_URL', 'http://127.0.0.1:4100'),
                  session_token=os.environ.get('DIRECTOR_WORKBENCH_SESSION')).run(transport='stdio')
