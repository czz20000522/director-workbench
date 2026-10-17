from backend import app as backend


def test_interrupt_ack_does_not_mean_stopped(monkeypatch):
    writes, calls = [], []
    def request(path, body=None):
        calls.append((path, body))
        return {'queue_running': [[0, 'ours']], 'queue_pending': []} if path == '/queue' else {}
    monkeypatch.setattr(backend, 'request_json', request)
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: writes.append(fields))
    assert not backend.advance_task_stop({'id': 'task', 'status': 'stop_requested'}, 'ours')
    assert writes[-1]['status'] == 'stopping'
    assert calls[-1] == ('/interrupt', {'prompt_id': 'ours'})


def test_missing_receipt_never_interrupts_unrelated_task(monkeypatch):
    writes, calls = [], []
    def request(path, body=None):
        calls.append(path)
        return {'queue_running': [[0, 'other']], 'queue_pending': []}
    monkeypatch.setattr(backend, 'request_json', request)
    monkeypatch.setattr(backend, 'history_done', lambda _: None)
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: writes.append(fields))
    assert backend.advance_task_stop({'id': 'task', 'status': 'stopping'}, 'ours')
    assert writes[-1]['status'] == 'needs_reconcile'
    assert calls == ['/queue']


def test_terminal_error_confirms_stop(monkeypatch):
    writes = []
    monkeypatch.setattr(backend, 'request_json', lambda *args: {'queue_running': [], 'queue_pending': []})
    monkeypatch.setattr(backend, 'history_done', lambda _: {'status': 'error', 'outputs': []})
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: writes.append(fields))
    assert backend.advance_task_stop({'id': 'task', 'status': 'stopping'}, 'ours')
    assert writes[-1]['status'] == 'stopped'
