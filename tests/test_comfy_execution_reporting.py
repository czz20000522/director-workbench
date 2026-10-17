import json
import time
from urllib.error import URLError

import pytest

from backend import app as backend


def error_receipt():
    return {'status': 'error', 'outputs': [], 'raw_status': {'status_str': 'error', 'messages': [
        ['execution_start', {'prompt_id': 'ours'}],
        ['execution_error', {'node_id': '12', 'node_type': 'KSampler', 'exception_type': 'RuntimeError',
                             'exception_message': 'CUDA out of memory\n  allocation failed',
                             'traceback': ['private traceback'], 'current_inputs': {'secret': 'not displayed'}}],
    ]}}


@pytest.mark.parametrize('payload,label', [({}, '视频生成'), ({'kind': 'keyframe'}, '关键帧生成'),
    ({'asset_id': 'MASTER', 'assembly_asset_id': 'MASTER'}, '成片装配'),
    ({'asset_id': 'S01', 'assembly_asset_id': 'MASTER'}, '视频生成')])
def test_execution_error_summary_preserves_type_and_omits_bulk_diagnostics(payload, label):
    receipt=error_receipt()
    original=json.dumps(receipt)
    summary=backend.comfy_execution_error(payload, receipt)
    assert summary.startswith('ComfyUI '+label+'失败：')
    assert 'KSampler' in summary and 'RuntimeError' in summary and 'CUDA out of memory allocation failed' in summary
    assert 'private traceback' not in summary and 'secret' not in summary and '\n' not in summary
    assert json.dumps(receipt)==original


@pytest.mark.parametrize('raw', [None, [], 'bad', {}, {'messages': None}, {'messages': {}},
    {'messages': [None, [], ['execution_error'], ['execution_error', None], ['other', {}], ['execution_error', {}, 'extra']]}])
def test_malformed_error_receipt_has_stable_fallback(raw):
    assert backend.comfy_execution_error({}, {'raw_status': raw})=='ComfyUI 视频生成失败'


def test_last_error_wins_and_long_summary_is_bounded():
    done=error_receipt()
    done['raw_status']['messages'].append(['execution_error', {'exception_message':'latest '+('x'*5000)}])
    summary=backend.comfy_execution_error({}, done)
    assert 'latest' in summary and 'CUDA' not in summary and len(summary)<1100


@pytest.fixture
def task_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', tmp_path/'tasks.sqlite3')
    monkeypatch.setattr(backend, 'PRIVATE_WORKSPACES', None)
    with backend.db() as connection:
        connection.execute('INSERT INTO tasks (id,asset_id,status,prompt_id,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?,?)',
                           ('task','S01','queued','ours',time.time(),time.time(),'{}','test'))
        connection.commit()
    return tmp_path


def test_matching_running_prompt_advances_once(task_db, monkeypatch):
    monkeypatch.setattr(backend,'request_json',lambda _: {'queue_running': [[0,'other'],[1,'ours']], 'queue_pending': []})
    backend.update_comfy_execution_state('task','ours')
    first=backend.get_task('task')
    assert first['status']=='running'
    backend.update_comfy_execution_state('task','ours')
    assert backend.get_task('task')['updated_at']==first['updated_at']


@pytest.mark.parametrize('queue', [None, [], {}, {'queue_running':None}, {'queue_running':[]},
    {'queue_running':[], 'queue_pending':[[0,'ours']]}, {'queue_running':[[0,'other']]},
    {'queue_running':[None,[],['ours'],{'prompt_id':'ours'}]}])
def test_no_positive_running_evidence_does_not_advance(task_db, monkeypatch, queue):
    monkeypatch.setattr(backend,'request_json',lambda _: queue)
    backend.update_comfy_execution_state('task','ours')
    assert backend.get_task('task')['status']=='queued'


@pytest.mark.parametrize('exc', [OSError('offline'), ValueError('bad json'), URLError('offline')])
def test_queue_network_failure_is_not_task_failure(task_db,monkeypatch,exc):
    def request(_): raise exc
    monkeypatch.setattr(backend,'request_json',request)
    backend.update_comfy_execution_state('task','ours')
    assert backend.get_task('task')['status']=='queued'


@pytest.mark.parametrize('status,stop,prompt', [('stop_requested',0,'ours'),('stopping',0,'ours'),
    ('succeeded',0,'ours'),('failed',0,'ours'),('stopped',0,'ours'),('needs_reconcile',0,'ours'),
    ('queued',1,'ours'),('queued',0,'different')])
def test_queue_poll_preserves_stop_terminal_and_original_receipt(task_db,monkeypatch,status,stop,prompt):
    with backend.db() as connection:
        connection.execute('UPDATE tasks SET status=?,stop_requested=?,prompt_id=? WHERE id=?',(status,stop,prompt,'task'))
        connection.commit()
    monkeypatch.setattr(backend,'request_json',lambda _: {'queue_running':[[0,'ours']]})
    backend.update_comfy_execution_state('task','ours')
    task=backend.get_task('task')
    assert task['status']==status and task['prompt_id']==prompt


@pytest.mark.parametrize('kind', ['video','keyframe','assembly'])
def test_workflow_monitor_observes_running_then_records_original_error(task_db,monkeypatch,kind):
    workflow=task_db/'graph.json'
    workflow.write_text('{}')
    payload={'workflow':str(workflow),'asset_id':'S01','assembly_asset_id':'MASTER'}
    if kind=='keyframe':payload['kind']='keyframe'
    if kind=='assembly':payload['asset_id']='MASTER'
    done=error_receipt()
    observations=[]
    calls=[]
    def request(path, body=None):
        calls.append(path)
        if path=='/prompt':return {'prompt_id':'ours'}
        if path=='/queue':return {'queue_running':[[0,'ours']], 'queue_pending':[]}
        if path=='/history/ours':return {}
        raise AssertionError(path)
    def history(*args):
        observations.append(backend.get_task('task')['status'])
        return None if len(observations)==1 else done
    monkeypatch.setattr(backend,'request_json',request)
    monkeypatch.setattr(backend,'history_done',history)
    monkeypatch.setattr(backend.keyframes,'read_result',history)
    monkeypatch.setattr(backend,'sample_resources',lambda _: None)
    monkeypatch.setattr(backend.time,'sleep',lambda _: None)
    backend.run_workflow_submission('task',payload)
    task=backend.get_task('task')
    assert 'running' in observations
    assert task['status']=='failed' and task['result']==done
    assert task['error']==backend.comfy_execution_error(payload,done)
    assert calls.count('/prompt')==1


def test_running_task_blocks_resource_release_and_batch_stop_reaches_it(task_db):
    from fastapi import HTTPException
    with backend.db() as connection:
        connection.execute("UPDATE tasks SET status='running',batch_id='batch' WHERE id='task'")
        connection.commit()
    assert 'running' in backend.RELEASE_BLOCKING_STATUSES
    with pytest.raises(HTTPException) as rejected:
        backend.ensure_no_release_blocking_tasks('回收资源',gpu_only=True)
    assert rejected.value.status_code==409
    result=backend.stop_project_batch_tasks('test','batch')
    assert result['tasks'][0]['status']=='stop_requested'
    assert backend.get_task('task')['stop_requested']==1


def test_restart_preserves_running_receipt_for_reconciliation(task_db):
    with backend.db() as connection:
        connection.execute("UPDATE tasks SET status='running' WHERE id='task'")
        connection.commit()
    backend.mark_orphans()
    task=backend.get_task('task')
    assert task['status']=='needs_reconcile' and task['prompt_id']=='ours'
    with backend.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
