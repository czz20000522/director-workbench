import io
from urllib.error import HTTPError
import pytest
from backend import app as backend


@pytest.mark.parametrize('phase', ['submit', 'monitor'])
def test_connection_loss_keeps_task_for_reconciliation(tmp_path, monkeypatch, phase):
    workflow = tmp_path / 'graph.json'
    workflow.write_text('{}')
    writes = []
    def request(path, body=None):
        assert body['prompt_id'] == 'task'
        assert any(item.get('prompt_id') == 'task' for item in writes)
        if phase == 'submit':
            raise OSError('lost response')
        return {'prompt_id': 'existing-prompt'}
    monkeypatch.setattr(backend, 'request_json', request)
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: writes.append(fields))
    monkeypatch.setattr(backend, 'get_task', lambda _: {'stop_requested': False})
    monkeypatch.setattr(backend, 'sample_resources', lambda _: None)
    def fail_history(_):
        raise OSError('history unavailable')
    monkeypatch.setattr(backend, 'history_done', fail_history)
    backend.run_workflow_submission('task', {'workflow': str(workflow)})
    assert writes[-1]['status'] == 'needs_reconcile'
    assert any(item.get('prompt_id') == 'task' for item in writes)
    if phase == 'monitor':
        assert any(item.get('prompt_id') == 'existing-prompt' for item in writes)


def test_explicit_input_rejection_is_failed(tmp_path, monkeypatch):
    workflow = tmp_path / 'graph.json'
    workflow.write_text('{}')
    writes = []
    def reject(*args):
        raise HTTPError('http://localhost/prompt', 400, 'invalid', {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(backend, 'request_json', reject)
    monkeypatch.setattr(backend, 'set_task', lambda task_id, **fields: writes.append(fields))
    backend.run_workflow_submission('task', {'workflow': str(workflow)})
    assert writes[-1]['status'] == 'failed'
    assert writes[-1]['prompt_id'] is None
