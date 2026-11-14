"""绘图引擎预热接口：必须立刻返回、可重复调用，并且不阻塞提交。"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from backend import app as backend
from backend.routers import engine as engine_router


def test_warm_returns_immediately_and_starts_only_once(monkeypatch):
    calls: list[int] = []
    release = threading.Event()

    monkeypatch.setattr(engine_router.comfy_engine, 'online', lambda timeout=2.0: False)

    def fake_ensure() -> dict:
        calls.append(1)
        release.wait(5)
        return {'online': True, 'started': True}

    monkeypatch.setattr(engine_router.comfy_engine, 'ensure_online', fake_ensure)
    client = TestClient(backend.app)

    started = time.monotonic()
    first = client.post('/api/engine/warm').json()
    assert time.monotonic() - started < 2, '预热接口不能等引擎起来才返回'
    assert first['online'] is False and first['starting'] is True

    # 引擎还在起：再点一次不该叠第二个启动线程。
    second = client.post('/api/engine/warm').json()
    assert second['starting'] is True
    release.set()
    deadline = time.monotonic() + 5
    while engine_router._STARTING.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [1]


def test_warm_is_a_noop_when_the_engine_is_already_up(monkeypatch):
    monkeypatch.setattr(engine_router.comfy_engine, 'online', lambda timeout=2.0: True)

    def must_not_start() -> dict:
        raise AssertionError('引擎已经在线，不该再拉一个')

    monkeypatch.setattr(engine_router.comfy_engine, 'ensure_online', must_not_start)
    body = TestClient(backend.app).post('/api/engine/warm').json()
    assert body == {'online': True, 'starting': False, 'error': ''}


def test_status_reports_engine_state(monkeypatch):
    monkeypatch.setattr(engine_router.comfy_engine, 'online', lambda timeout=2.0: True)
    body = TestClient(backend.app).get('/api/engine/status').json()
    assert body['online'] is True
    assert body['starting'] is False
