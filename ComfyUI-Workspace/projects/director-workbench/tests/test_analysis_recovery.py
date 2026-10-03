import json
from pathlib import Path

from backend import app as backend
from test_reverse_workbench import reverse_client, seed_gpu_waiter, prepare_semantic


def orphan(path, *, receipt=True):
    job = backend.background_jobs.create(backend.analysis_job_root(), 'project-a', 'ref-test')
    result = path.parent / 'semantic-results' / f"{job['id']}.json"
    receipt_path = result.with_suffix('.receipt.json')
    job = backend.background_jobs.update(backend.analysis_job_root(), job, owner='old-server', status='running', result_path=str(result), receipt_path=str(receipt_path))
    result.parent.mkdir(exist_ok=True)
    result.write_text(json.dumps({'artifacts': {'story': {'summary': '恢复的模型候选', 'confidence': .5}}}), encoding='utf-8')
    if receipt:
        receipt_path.write_text(json.dumps({'job_id': job['id'], 'status': 'succeeded', 'analysis_path': str(path.resolve()), 'result_path': str(result.resolve())}), encoding='utf-8')
    return job, result, receipt_path


endpoint = '/api/projects/project-a/reverse-analyses/ref-test/semantic-jobs'


def test_recovery_merges_latest_review_without_launching_model(reverse_client, monkeypatch):
    client, _, path = reverse_client
    job, _, _ = orphan(path)
    document = json.loads(path.read_text(encoding='utf-8'))
    document['artifacts']['story'].update(summary='人工确认的版本', user_status='reviewed')
    path.write_text(json.dumps(document), encoding='utf-8')
    def no_launch(*args, **kwargs): raise AssertionError('Must not restart model')
    monkeypatch.setattr(backend, 'run_reference_command', no_launch)
    response = client.post(endpoint + '/reconcile')
    assert response.status_code == 200, response.text
    assert response.json()['job']['id'] == job['id']
    assert response.json()['job']['status'] == 'succeeded'
    result = json.loads(path.read_text(encoding='utf-8'))
    assert result['artifacts']['story']['summary'] == '人工确认的版本'
    assert result['artifacts']['story']['model_candidate']['summary'] == '恢复的模型候选'
    before = path.read_bytes()
    assert client.post(endpoint + '/reconcile').status_code == 200
    assert path.read_bytes() == before
    backend.ensure_no_unresolved_analysis_jobs()


def test_missing_receipt_does_not_assume_process_stopped(reverse_client):
    client, _, path = reverse_client
    orphan(path, receipt=False)
    before = path.read_bytes()
    assert client.post(endpoint + '/reconcile').status_code == 409
    assert client.get(endpoint + '/current').json()['job']['status'] == 'needs_reconcile'
    assert path.read_bytes() == before
    task_id = seed_gpu_waiter()
    assert backend.TASK_SCHEDULER.tick(threaded=False) == []
    assert backend.get_task(task_id)['status'] == 'scheduler_waiting'
    assert backend.TASK_SCHEDULER.queue_info(task_id)['reason']['code'] == 'analysis_needs_reconcile'


def test_wrong_receipt_and_missing_result_preserve_analysis(reverse_client):
    client, _, path = reverse_client
    _, result, receipt_path = orphan(path)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    receipt['job_id'] = 'another-task'
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    before = path.read_bytes()
    assert client.post(endpoint + '/reconcile').status_code == 409
    receipt['job_id'] = result.stem
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    result.write_text('{invalid', encoding='utf-8')
    assert client.post(endpoint + '/reconcile').status_code == 502
    assert path.read_bytes() == before


def test_durable_semantic_recovery_matches_frozen_source_and_preserves_latest_review(reverse_client, monkeypatch, tmp_path):
    client, _, path = reverse_client
    prepare_semantic(monkeypatch, tmp_path)
    response = client.post(endpoint)
    assert response.status_code == 202
    task_id = response.json()['job']['id']
    task = backend.get_task(task_id)
    frozen_input = Path(task['payload']['analysis_path'])
    assert frozen_input != path and frozen_input.read_bytes() == path.read_bytes()
    backend.set_task(task_id, status='needs_reconcile')
    job = backend.background_jobs.update(backend.analysis_job_root(), task['payload'], owner='previous-server', status='running')
    candidate = Path(job['result_path'])
    candidate.parent.mkdir(exist_ok=True)
    candidate.write_text(json.dumps({'artifacts': {'story': {'summary': '冻结请求的模型候选'}}}), encoding='utf-8')
    Path(job['receipt_path']).write_text(json.dumps({'job_id': task_id, 'analysis_path': str(frozen_input.resolve()),
        'result_path': str(candidate.resolve()), 'status': 'succeeded'}), encoding='utf-8')
    document = json.loads(path.read_text(encoding='utf-8'))
    document['artifacts']['story'].update(summary='排队后导演的新校订', user_status='reviewed')
    path.write_text(json.dumps(document), encoding='utf-8')
    monkeypatch.setattr(backend, 'execute_scheduled_task', lambda task: (_ for _ in ()).throw(AssertionError('Must never replay ambiguous request')))
    assert backend.TASK_SCHEDULER.tick(threaded=False) == []
    recovered = client.post(endpoint + '/reconcile')
    assert recovered.status_code == 200, recovered.text
    assert backend.get_task(task_id)['status'] == 'succeeded'
    result = json.loads(path.read_text(encoding='utf-8'))['artifacts']['story']
    assert result['summary'] == '排队后导演的新校订'
    assert result['model_candidate']['summary'] == '冻结请求的模型候选'
    before = path.read_bytes()
    assert client.post(endpoint + '/reconcile').status_code == 200
    assert path.read_bytes() == before


def test_worker_writes_matching_receipt_after_candidate(tmp_path, monkeypatch):
    from tools import reference_semantic as worker
    analysis = tmp_path / 'analysis.json'
    analysis.write_text('{}', encoding='utf-8')
    output = tmp_path / 'candidate.json'
    receipt = tmp_path / 'receipt.json'
    monkeypatch.setattr('sys.argv', ['worker', '--analysis', str(analysis), '--result-output', str(output), '--completion-receipt', str(receipt), '--job-id', 'job-test'])
    monkeypatch.setattr(worker, 'run_analysis', lambda *args: {'artifacts': {'story': {'summary': 'candidate'}}})
    assert worker.main() == 0
    assert json.loads(output.read_text(encoding='utf-8'))['artifacts']['story']['summary'] == 'candidate'
    assert json.loads(receipt.read_text(encoding='utf-8')) == {'job_id': 'job-test', 'analysis_path': str(analysis.resolve()), 'result_path': str(output.resolve()), 'status': 'succeeded'}


def test_worker_failure_never_emits_success_receipt(tmp_path, monkeypatch):
    import pytest
    from tools import reference_semantic as worker
    analysis = tmp_path / 'analysis.json'
    analysis.write_text('{}', encoding='utf-8')
    receipt = tmp_path / 'receipt.json'
    monkeypatch.setattr('sys.argv', ['worker', '--analysis', str(analysis), '--result-output', str(tmp_path / 'candidate.json'), '--completion-receipt', str(receipt), '--job-id', 'job-test'])
    def fail(*args): raise RuntimeError('model failed')
    monkeypatch.setattr(worker, 'run_analysis', fail)
    with pytest.raises(RuntimeError): worker.main()
    assert not receipt.exists()
