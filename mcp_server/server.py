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
    from . import tools_creative, tools_generation, tools_media, tools_reference
else:
    from models import ScriptScene, ShotChanges, ShotMaterials
    import tools_creative, tools_generation, tools_media, tools_reference


class _Client:
    def __init__(self, request: Callable[..., Any], project_path: Callable[[str], str], path_id: Callable[[str, str], str]):
        self.request = request
        self.project_path = project_path
        self.path_id = path_id


def create_server(base_url: str, transport: httpx.AsyncBaseTransport | None = None, *, session_token: str | None = None,
                  session_token_provider: Callable[[], str | None] | None = None,
                  transport_provider: Callable[[], httpx.AsyncBaseTransport] | None = None) -> MCPServer:
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

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = session_headers
        if session_token_provider is not None:
            token = session_token_provider()
            if not token:
                return {'ok':False,'error':{'code':'authentication_required','http_status':401,'message':'请重新登录本人工作台账号'}}
            headers = {'Authorization':'Bearer '+token}
        try:
            async with httpx.AsyncClient(base_url=base_url, transport=transport_provider() if transport_provider else transport, timeout=30, trust_env=False, follow_redirects=False, headers=headers) as client:
                response = await client.request(method, path, **kwargs)
            data = response.json()
        except httpx.RequestError:
            return {'ok': False, 'error': {'code': 'workbench_unavailable', 'message': '工作台请求未取得回执；写入请求请先读取最新记录核对，不要直接重发', 'retryable': method == 'GET', 'outcome_unknown': method != 'GET'}}
        except ValueError:
            return {'ok': False, 'error': {'code': 'invalid_response', 'message': '工作台未返回 JSON', 'retryable': False}}
        if not response.is_success:
            return {'ok': False, 'error': {'code': 'workbench_http_error', 'http_status': response.status_code, 'detail': data.get('detail', data), 'retryable': False}}
        return {'ok': True, 'data': data}

    def project_path(project_id: str) -> str:
        if not project_id or project_id in {'.', '..'} or '/' in project_id or '\\' in project_id:
            raise ValueError('必须提供有效的作品 ID')
        return '/api/projects/' + quote(project_id, safe='')

    def path_id(value: str, label: str) -> str:
        if not value or value in {'.', '..'} or '/' in value or chr(92) in value or chr(0) in value:
            raise ValueError(f'必须提供有效{label} ID')
        return quote(value, safe='')

    client = _Client(request, project_path, path_id)
    tools_creative.register(server, client, read_only=read_only, write_record=write_record)
    tools_reference.register(server, client, read_only=read_only, write_record=write_record)
    tools_generation.register(server, client, read_only=read_only, write_record=write_record)
    tools_media.register(server, client, read_only=read_only, write_record=write_record)
    return server


if __name__ == '__main__':
    create_server(os.environ.get('DIRECTOR_WORKBENCH_URL', 'http://127.0.0.1:4100'),
                  session_token=os.environ.get('DIRECTOR_WORKBENCH_SESSION')).run(transport='stdio')
