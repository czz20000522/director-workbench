from __future__ import annotations

from pathlib import Path

import pytest

from backend import comfy_engine


class FakeProcess:
    pid = 4242

    def __init__(self, code: int | None = None):
        self.code = code

    def poll(self) -> int | None:
        return self.code


def test_online_engine_is_not_started_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden():
        pytest.fail('a reachable engine must not be started again')

    monkeypatch.setattr(comfy_engine, 'online', lambda timeout=2.0: True)
    monkeypatch.setattr(comfy_engine, '_start', forbidden)
    assert comfy_engine.ensure_online() == {'online': True, 'started': False}


def test_offline_engine_starts_and_waits_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter([False, False, True])
    monkeypatch.setattr(comfy_engine, 'online', lambda timeout=2.0: next(states))
    monkeypatch.setattr(comfy_engine, '_start', lambda: FakeProcess())
    monkeypatch.setattr(comfy_engine.time, 'sleep', lambda seconds: None)
    assert comfy_engine.ensure_online() == {'online': True, 'started': True, 'pid': 4242}


def test_engine_that_exits_reports_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comfy_engine, 'online', lambda timeout=2.0: False)
    monkeypatch.setattr(comfy_engine, '_start', lambda: FakeProcess(7))
    with pytest.raises(RuntimeError, match='code 7'):
        comfy_engine.ensure_online()


def test_missing_portable_python_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(comfy_engine, 'COMFY_ROOT', str(tmp_path))
    with pytest.raises(RuntimeError, match='找不到本地 ComfyUI'):
        comfy_engine.launch_command()


def test_engine_failure_fails_task_before_submission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backend import app as backend

    workflow = tmp_path / 'graph.json'
    workflow.write_text('{"1": {"class_type": "PreviewImage", "inputs": {}}}', encoding='utf-8')
    recorded: dict = {}
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: recorded.update(fields))
    monkeypatch.setattr(backend, '_task_log', lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, 'request_json', lambda *args, **kwargs: pytest.fail('must not submit'))
    monkeypatch.setattr(comfy_engine, 'ensure_online', lambda: (_ for _ in ()).throw(RuntimeError('引擎没起来')))

    backend.run_workflow_submission('task-1', {'workflow': str(workflow)})

    assert recorded['status'] == 'failed'
    assert '引擎没起来' in recorded['error']
