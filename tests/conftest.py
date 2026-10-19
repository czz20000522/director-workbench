"""Shared pytest fixtures.

The suite historically reused fixtures by importing them from sibling test modules,
which made test files depend on each other's module layout.  Base fixtures and the
isolated preset factory live here; plain helpers that take a client/project live in
``support.py``.  Standalone browser bridges retain their explicit fixture ``__wrapped__``
calls because they run outside pytest.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend


@pytest.fixture
def preset_client_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the isolated preset client used by both normal and host-tool tests."""
    def factory():
        project = tmp_path / 'ComfyUI-Workspace' / 'project'
        projects = project / 'projects'
        projects.mkdir(parents=True)
        (projects / 'catalog.json').write_text('{"projects": []}', encoding='utf-8')
        for name, value in {
            'ROOT': tmp_path / 'data-root', 'PROJECT': project, 'PROJECTS_ROOT': projects,
            'PROJECT_CATALOG_PATH': projects / 'catalog.json', 'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
            'DB_PATH': tmp_path / 'state.sqlite3', 'SNAPSHOT_ROOT': project / 'snapshots', 'INPUT_ROOT': tmp_path / 'input',
        }.items():
            monkeypatch.setattr(backend, name, value)
        for name in ('CURRENT_PROJECT', 'CURRENT_PROJECT_ID', 'PLAN_PATH', 'MEDIA', 'TASK_TEMPLATES', 'PIPELINE_STAGES'):
            monkeypatch.setattr(backend, name, getattr(backend, name))
        monkeypatch.setattr(backend, 'require_submission_capacity', lambda: {})

        class NoopThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        monkeypatch.setattr(backend.threading, 'Thread', NoopThread)
        client = TestClient(backend.app)
        response = client.post('/api/projects/create', json={
            'series': '测试', 'title': '新作', 'project_id': 'preset-test', 'creation_mode': 'advanced',
        })
        assert response.status_code == 200, response.text
        return client, project

    return factory


@pytest.fixture
def preset_client(preset_client_factory):
    return preset_client_factory()


from test_private_workbench import private_workbench  # noqa: F401
from test_assembly_edit import edit_client  # noqa: F401
from test_reverse_workbench import reverse_client  # noqa: F401
from test_scheduler_app import queued_private_app  # noqa: F401
from test_task_reconciliation import reconcile_client  # noqa: F401
