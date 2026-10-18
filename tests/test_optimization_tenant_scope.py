"""A task owned by another account must not block a mutation it cannot disturb.

The release check used to scan every task row, so user002 could not delete their own
work (or install a preset, or delete a shot) while user001 had a GPU task running.  The
owner's own task must still block the owner's mutation.
"""
import json
import sqlite3
import time

import pytest

from backend import app as backend
from backend.private_auth import Principal, current_principal
from backend.user_context import SessionIdentity, UserContext
from test_private_workbench import create


def install_tasks(tmp_path, monkeypatch, tasks):
    path = tmp_path / 'optimization-tasks.sqlite3'
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)')
    for identifier, status, payload in tasks:
        connection.execute('INSERT INTO tasks(id,status,payload) VALUES (?,?,?)',
                           (identifier, status, json.dumps(payload)))
    connection.commit()
    connection.close()

    def connect():
        created = sqlite3.connect(path)
        created.row_factory = sqlite3.Row
        return created

    monkeypatch.setattr(backend, 'db', connect)


def principal_for(username):
    return Principal(SessionIdentity(username, time.time() + 60), 'token-' + username, False)


def test_private_session_is_blocked_only_by_its_own_tasks(tmp_path, monkeypatch):
    install_tasks(tmp_path, monkeypatch, [
        ('task-a', 'running', {'kind': 'video', 'owner_user': 'user001', 'project_id': 'p1'}),
        ('task-b', 'running', {'kind': 'video', 'owner_user': 'user002', 'project_id': 'p2'}),
    ])
    token = current_principal.set(principal_for('user001'))
    try:
        with pytest.raises(Exception) as blocked:
            backend.ensure_no_release_blocking_tasks('删除系列')
        assert getattr(blocked.value, 'status_code', None) == 409
        # The other account's project is not this session's concern.
        backend.ensure_no_release_blocking_tasks('删除系列', project_id='p2')
        backend.ensure_no_release_blocking_tasks('开始参考内容分析', gpu_only=True, project_id='p2')
    finally:
        current_principal.reset(token)

    token = current_principal.set(principal_for('user002'))
    try:
        with pytest.raises(Exception) as blocked:
            backend.ensure_no_release_blocking_tasks('删除作品', project_id='p2')
        assert getattr(blocked.value, 'status_code', None) == 409
        backend.ensure_no_release_blocking_tasks('删除作品', project_id='p1')
    finally:
        current_principal.reset(token)


def test_legacy_mode_keeps_the_whole_table_check(tmp_path, monkeypatch):
    install_tasks(tmp_path, monkeypatch, [
        ('task-a', 'running', {'kind': 'video', 'owner_user': 'user001', 'project_id': 'p1'}),
    ])
    # No private session is installed, so every visible task still owns the resources.
    with pytest.raises(Exception) as blocked:
        backend.ensure_no_release_blocking_tasks('删除系列')
    assert getattr(blocked.value, 'status_code', None) == 409


def test_gpu_only_ignores_cpu_tasks(tmp_path, monkeypatch):
    install_tasks(tmp_path, monkeypatch, [
        ('task-cpu', 'running', {'kind': 'audio_edit', 'owner_user': 'user001', 'project_id': 'p1'}),
    ])
    token = current_principal.set(principal_for('user001'))
    try:
        backend.ensure_no_release_blocking_tasks('开始参考内容分析', gpu_only=True)
        with pytest.raises(Exception) as blocked:
            backend.ensure_no_release_blocking_tasks('删除系列')
        assert getattr(blocked.value, 'status_code', None) == 409
    finally:
        current_principal.reset(token)


@pytest.mark.host_tool
def test_deleting_own_work_is_not_blocked_by_another_accounts_task(private_workbench):
    a, b, ownership, _ = private_workbench
    mine = create(a)
    theirs = create(b)
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,project_id) '
            'VALUES (?,?,?,?,?,?,?)',
            ('cross-account-task', 'S01', 'running', time.time(), time.time(),
             json.dumps({'kind': 'video', 'owner_user': 'user001', 'project_id': mine['id']}, ensure_ascii=False),
             mine['id']))
        connection.commit()

    folder = UserContext('user002', ownership.layout).project(theirs['id'])
    path = f'/api/private/series/{folder.parent.name}/works/{folder.name}'
    preview = b.post(path + '/deletion-plan')
    assert preview.status_code == 200, preview.text
    deleted = b.request('DELETE', path, json={'plan_id': preview.json()['plan_id'], 'confirm': folder.name})
    assert deleted.status_code == 200, deleted.text
    assert not folder.exists()

    # The owner's own running task still blocks the owner's delete.
    owner_folder = UserContext('user001', ownership.layout).project(mine['id'])
    owner_path = f'/api/private/series/{owner_folder.parent.name}/works/{owner_folder.name}'
    owner_preview = a.post(owner_path + '/deletion-plan')
    assert owner_preview.status_code == 200, owner_preview.text
    blocked = a.request('DELETE', owner_path,
                        json={'plan_id': owner_preview.json()['plan_id'], 'confirm': owner_folder.name})
    assert blocked.status_code == 409
