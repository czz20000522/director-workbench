"""The remote MCP entry mounted on the workbench's own ASGI app.

The stdio server in `mcp_server/` carries the full tool set. This entry serves
the same tools over the SDK's Streamable HTTP transport, so a client that keeps
its own workbench bearer reaches the existing authenticated business routes
instead of an alternate implementation.
"""
from contextlib import AsyncExitStack, asynccontextmanager

import httpx

from mcp_server.server import create_server
from .mcp_access_config import transport_security

# The remote entry stays deliberately smaller than the stdio tool set: the
# creation path plus read-only guide settings. Everything else stays reachable
# through the authenticated HTTP API; publishing more means naming it here.
REMOTE_TOOLS = (
    'create_project', 'list_projects', 'read_project', 'create_shot', 'update_shot',
    'upload_material', 'preflight', 'submit_shot', 'get_submission_receipt', 'read_queue',
    'get_task', 'list_shot_versions', 'stop_generation', 'read_guide_settings',
)


async def prepare(remote):
    tools = await remote.list_tools()
    names = {tool.name for tool in tools}
    missing = sorted(set(REMOTE_TOOLS) - names)
    if missing:
        raise RuntimeError(f'Remote MCP allow-list names tools this server does not have: {missing}')
    for tool in tools:
        if tool.name not in REMOTE_TOOLS:
            remote.remove_tool(tool.name)


def install(app, *, http_app_provider, token_provider, environ=None):
    remote = create_server('http://localhost', httpx.ASGITransport(app=http_app_provider()),
                           session_token_provider=token_provider,
                           transport_provider=lambda: httpx.ASGITransport(app=http_app_provider()))
    mcp_app = remote.streamable_http_app(streamable_http_path='/', stateless_http=True, json_response=True,
                                         max_request_body_size=24 * 1024 * 1024,
                                         transport_security=transport_security(environ))
    app.mount('/mcp', mcp_app, name='director-mcp')
    app.state.remote_mcp = remote

    @asynccontextmanager
    async def serve_mcp():
        await prepare(remote)
        context = remote.session_manager.run()
        await context.__aenter__()
        app.state.remote_mcp_context = context
        try:
            yield
        finally:
            app.state.remote_mcp_context = None
            await context.__aexit__(None, None, None)

    # The session manager only stays open while the app's lifespan is running, so the app
    # owns entering and exiting this; see the lifespan in backend/app.py.
    lifespan_subsystems(app).append(serve_mcp)
    return remote


def lifespan_subsystems(app):
    """The list of context managers this app's lifespan enters, one per mounted subsystem.

    The workbench app builds this list and enters it from its own lifespan (backend/app.py).
    An app assembled somewhere else -- a test fixture mounting this entry onto a bare
    FastAPI, say -- has no such lifespan and would leave the session manager closed, so it
    gets one that enters whatever is appended here.
    """
    if hasattr(app.state, 'lifespan_subsystems'):
        return app.state.lifespan_subsystems
    subsystems = []
    app.state.lifespan_subsystems = subsystems
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        stack = AsyncExitStack()
        await stack.enter_async_context(inner(app_))
        for factory in subsystems:
            await stack.enter_async_context(factory())
        try:
            yield
        finally:
            await stack.aclose()

    app.router.lifespan_context = lifespan
    return subsystems
