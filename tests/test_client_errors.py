import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend import app as backend_app


def test_client_error_is_appended_and_readable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(backend_app, 'ROOT', tmp_path)
    client = TestClient(backend_app.app)

    response = client.post('/api/client-errors', json={
        'message': "Cannot read properties of undefined (reading 'name')",
        'stack': 'TypeError: ...\n    at KeyframePanel',
        'component_stack': 'at KeyframePanel',
        'url': 'http://127.0.0.1:4100/',
        'kind': 'render',
        # 前端将来加字段不该被 422 挡住，日志宁可多不可丢。
        'future_field': 'ignored',
    })

    assert response.status_code == 200, response.text
    assert response.json()['stored'] is True
    log = tmp_path / 'logs' / 'client-errors.log'
    entry = json.loads(log.read_text(encoding='utf-8').strip().splitlines()[-1])
    assert entry['message'].startswith('Cannot read properties')
    assert entry['kind'] == 'render'
    assert 'future_field' not in entry

    recent = client.get('/api/client-errors/recent?limit=5').json()['entries']
    assert recent[-1]['component_stack'] == 'at KeyframePanel'


def test_client_error_log_rolls_over_when_too_large(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(backend_app, 'ROOT', tmp_path)
    folder = tmp_path / 'logs'
    folder.mkdir(parents=True)
    target = folder / 'client-errors.log'
    target.write_bytes(b'x' * 2_000_001)

    client = TestClient(backend_app.app)
    assert client.post('/api/client-errors', json={'message': 'boom'}).json()['stored'] is True

    assert (folder / 'client-errors.log.1').is_file()
    assert 'boom' in target.read_text(encoding='utf-8')
