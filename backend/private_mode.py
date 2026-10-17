"""Opt-in authentication on the existing application, without another engine."""
from pathlib import Path
import json

from fastapi import HTTPException

from .private_auth import PrivateAuth
from .private_sessions import AccountStore, SessionManager
from .private_workspaces import PrivateWorkspaces
from .user_context import Layout, no_links


def install(backend, *, accounts: AccountStore, layout: Layout, origins: tuple[str, ...], administrators=(), secure_cookie=False):
    if backend.PRIVATE_WORKSPACES is not None:
        raise RuntimeError('Private mode is already installed')
    workspaces = PrivateWorkspaces(layout)
    sessions = SessionManager(accounts)
    backend.PRIVATE_WORKSPACES = workspaces

    def legacy_file_allowed(candidate):
        # Only administrator-owned legacy work registrations grant access;
        # being an administrator is not permission to browse arbitrary disks.
        def resolve(value):
            return no_links(backend.registered_candidate(value))

        private_ids = set(workspaces.read()['projects'])
        broad_roots = {backend.ROOT.resolve(), backend.PROJECT.resolve(), backend.INPUT_ROOT.resolve(), backend.OUTPUT_ROOT.resolve(), backend.DIRECTOR_WORKSPACES_ROOT.resolve()}
        for entry in backend.project_entries():
            if entry.get('id') in private_ids:
                continue
            try:
                manifest = backend.load_project_manifest(entry['id'])
                workspace = resolve(manifest['workspace_root']) if manifest.get('workspace_root') else None
                if workspace and workspace not in broad_roots and candidate.is_relative_to(workspace):
                    return True
                values = [manifest.get('plan_path'), *(manifest.get('media') or {}).values(),
                          (manifest.get('production_preset') or {}).get('workflow')]
                for asset in manifest.get('assets', []):
                    if isinstance(asset, dict):
                        values.extend(backend.asset_record_paths(asset))
                plan_path = resolve(manifest['plan_path']) if manifest.get('plan_path') else None
                if plan_path and plan_path.is_file():
                    plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
                    for segment in plan.get('segments', []):
                        values.extend([segment.get('workflow'), (segment.get('video') or {}).get('path'),
                                       *(segment.get('keyframes') or {}).values(), *(segment.get('audio') or {}).values()])
                    assembly = plan.get('assembly') or {}
                    values.extend([assembly.get('output'), assembly.get('workflow')])
                # Frozen task graphs can be outside the legacy work directory.
                # Grant exact registered files, never the global snapshots root.
                with backend.DB_LOCK, backend.db() as connection:
                    rows = connection.execute('SELECT payload FROM tasks WHERE project_id=?', (entry['id'],)).fetchall()
                for row in rows:
                    payload = json.loads(row['payload'])
                    values.extend([payload.get('workflow'), payload.get('source_workflow'),
                                   (payload.get('execution_snapshot') or {}).get('api_graph')])
                for value in values:
                    if value and resolve(value) == candidate:
                        return True
            except (ValueError, OSError, RuntimeError, HTTPException, KeyError):
                continue
        return False

    workspaces.legacy_file_allowed = legacy_file_allowed

    def authorize(request, principal):
        path = request.url.path
        if path in {'/mcp','/mcp/'} or path.startswith(('/api/agent/', '/api/assistant/')):
            # Presentation checks owner/tab inside its service; MCP forwards
            # each real bearer to the existing project HTTP authorization.
            return None
        if path == '/api/private/series' or path.startswith('/api/private/series/'):
            return None
        if path.startswith('/api/projects/') and path not in {'/api/projects/create', '/api/projects/select', '/api/projects/import'}:
            project_id = path.split('/')[3]
            try:
                workspaces.require(project_id, principal)
            except HTTPException as exc:
                return exc.status_code, exc.detail
            # Custom executable graphs are administrator-maintained; ordinary
            # users install vetted presets and alter exposed recipe inputs.
            if path.endswith('/pipeline/stages') and not principal.administrator:
                return 403, '自定义制作步骤由管理员维护'
            return None
        allowed = {'/api/project', '/api/projects', '/api/projects/create', '/api/projects/select',
                   '/api/material-library', '/material-file', '/media-file', '/workflow-file',
                   '/api/health', '/api/status', '/api/production-presets', '/api/settings/guide', '/api/queue',
                   '/api/keyframe-capability', '/api/speech-capability', '/api/media-capability', '/api/models/reverse-analysis',
                   '/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc'}
        if path in allowed:
            if path in {'/api/material-library', '/material-file'}:
                try:
                    workspaces.require(request.query_params.get('project_id', ''), principal)
                except HTTPException as exc:
                    return exc.status_code, exc.detail
            return None
        if path == '/api/resources/release' and principal.administrator:
            return None
        # Legacy globals and arbitrary local-folder imports cannot cross into
        # private mode; no anonymous/local-admin fallthrough.
        return 403, '请使用当前作品入口'

    backend.app.add_middleware(PrivateAuth, sessions=sessions, origins=origins,
                               authorize=authorize, administrators=tuple(administrators), secure_cookie=secure_cookie)
    return sessions, workspaces
