"""Uploads are bounded, streamed to a prepared file and leave no partial material behind."""
import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from backend import app as backend
from backend.user_context import UserContext
from test_private_workbench import create


def test_per_file_limit_returns_413_and_removes_the_partial_file(monkeypatch, private_workbench):
    a, _, ownership, _ = private_workbench
    monkeypatch.setattr(backend, 'MAX_UPLOAD_BYTES', 8)
    project = create(a)
    folder = UserContext('user001', ownership.layout).project(project['id'])
    uploads = folder / 'assets' / 'uploads'
    response = a.post(f"/api/projects/{project['id']}/upload",
                      files=[('files', ('clip.png', b'0123456789', 'image/png'))])
    assert response.status_code == 413, response.text
    assert '单个文件超过' in response.json()['detail']
    assert not uploads.is_dir() or list(uploads.iterdir()) == []
    # A rejected upload must not be registered as a material.
    assert backend.load_project_manifest(project['id']).get('assets') in (None, [])


def test_file_count_limit_returns_413(monkeypatch, private_workbench):
    a, _, _, _ = private_workbench
    monkeypatch.setattr(backend, 'MAX_UPLOAD_FILES', 1)
    project = create(a)
    response = a.post(f"/api/projects/{project['id']}/upload",
                      files=[('files', ('a.png', b'x', 'image/png')),
                             ('files', ('b.png', b'x', 'image/png'))])
    assert response.status_code == 413, response.text


def test_request_body_limit_rejects_before_fastapi_parses_the_body():
    application = FastAPI()
    handled = []

    @application.post('/api/projects/p1/upload')
    async def upload(payload: dict = Body(...)):
        handled.append(True)
        return {'ok': True}

    application.add_middleware(backend.RequestBodyLimit, max_bytes=10)
    response = TestClient(application).post(
        '/api/projects/p1/upload', content=b'x' * 40,
        headers={'content-type': 'application/octet-stream'})
    assert response.status_code == 413
    assert handled == []
