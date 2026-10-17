import json
import threading
import time

import pytest

from backend import app as backend
from backend.gpu_idle_release import IdleGPURelease


@pytest.fixture
def isolated_gpu_state(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr(backend, 'REFERENCE_DECOMPOSE_ACTIVE', 0)
    with backend.db() as connection:
        connection.commit()
    return tmp_path


def put_task(task_id, status, project_id, kind=None):
    payload = {'kind': kind} if kind else {}
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
            (task_id, 'S01', status, time.time(), time.time(), json.dumps(payload), project_id),
        )
        connection.commit()


def test_idle_grace_one_release_per_activity_and_restart():
    now = [0.0]
    calls = []
    busy = [None]
    lock = threading.RLock()
    make = lambda: IdleGPURelease(lock, lambda: busy[0], lambda: calls.append('free') or {},
                                   lambda: 1234, grace_seconds=120, retry_seconds=60, clock=lambda: now[0])
    controller = make()
    controller.poll()
    now[0] = 119
    controller.poll()
    assert calls == []
    now[0] = 120
    controller.poll()
    assert calls == ['free']
    assert controller.last_result['gpu_free_mib_before'] == 1234
    now[0] = 500
    controller.poll()
    assert calls == ['free']
    busy[0] = 'other user running'
    controller.poll()
    busy[0] = None
    now[0] = 619
    controller.poll()
    assert calls == ['free']
    now[0] = 620
    controller.poll()
    assert calls == ['free', 'free']
    # A restarted worker starts its own grace window, not an immediate /free.
    restarted = make()
    restarted.poll()
    assert calls == ['free', 'free']
    now[0] = 740
    restarted.poll()
    assert calls == ['free', 'free', 'free']


def test_failed_or_uncertain_check_never_releases_and_retry_is_bounded():
    now = [0.0]
    calls = []
    fail_check = [False]
    def inspect():
        if fail_check[0]:
            raise OSError('queue unavailable')
        return None
    def release():
        calls.append('attempt')
        if len(calls) == 1:
            raise OSError('connection reset')
        return {}
    controller = IdleGPURelease(threading.RLock(), inspect, release, lambda: None,
                                grace_seconds=10, retry_seconds=60, clock=lambda: now[0])
    now[0] = 10
    fail_check[0] = True
    controller.poll()
    assert calls == [] and controller.state == 'uncertain'
    fail_check[0] = False
    now[0] = 20
    controller.poll()
    assert calls == ['attempt'] and controller.state == 'retry'
    now[0] = 79
    controller.poll()
    assert calls == ['attempt']
    now[0] = 80
    controller.poll()
    assert calls == ['attempt', 'attempt'] and controller.released


def test_short_external_comfy_job_rearms_idle_after_release():
    now = [0.0]
    latest_history = ['older-prompt']
    calls = []
    controller = IdleGPURelease(threading.RLock(), lambda: None,
                                lambda: calls.append(now[0]) or {}, lambda: None,
                                grace_seconds=120, clock=lambda: now[0],
                                activity_token=lambda: latest_history[0])
    controller.poll()  # Baseline; the queue is empty at every poll in this test.
    now[0] = 120
    controller.poll()
    assert calls == [120] and controller.released
    # An external client starts and finishes a task between the 15-second polls.
    latest_history[0] = 'new-external-prompt'
    now[0] = 135
    controller.poll()
    assert calls == [120] and not controller.released
    now[0] = 254
    controller.poll()
    assert calls == [120]
    now[0] = 255
    controller.poll()
    assert calls == [120, 255]
    now[0] = 500
    controller.poll()
    assert calls == [120, 255]


def test_history_failure_is_uncertain_and_never_calls_free():
    now = [0.0]
    history_ok = [True]
    calls = []
    def token():
        if not history_ok[0]:
            raise OSError('history unavailable')
        return 'stable'
    controller = IdleGPURelease(threading.RLock(), lambda: None,
                                lambda: calls.append('free') or {}, lambda: None,
                                grace_seconds=10, clock=lambda: now[0], activity_token=token)
    controller.poll()
    now[0] = 10
    history_ok[0] = False
    controller.poll()
    assert calls == [] and controller.state == 'uncertain'
    history_ok[0] = True
    now[0] = 19
    controller.poll()
    assert calls == []
    now[0] = 20
    controller.poll()
    assert calls == ['free']


def test_global_probe_blocks_any_user_unknown_task_analysis_and_queue(isolated_gpu_state, monkeypatch):
    queue = {'queue_running': [], 'queue_pending': []}
    monkeypatch.setattr(backend, 'request_json', lambda path, *args, **kwargs: queue if path == '/queue' else {})
    put_task('cpu', 'running', 'alice', 'audio_edit')
    assert backend.gpu_idle_reason() is None
    put_task('gpu', 'needs_reconcile', 'bob')
    assert '待核对' in backend.gpu_idle_reason()
    backend.set_task('gpu', status='succeeded')
    job = backend.background_jobs.create(backend.analysis_job_root(), 'bob', 'reference')
    assert '参考' in backend.gpu_idle_reason()
    backend.background_jobs.update(backend.analysis_job_root(), job, status='succeeded')
    monkeypatch.setattr(backend, 'REFERENCE_DECOMPOSE_ACTIVE', 1)
    assert '参考' in backend.gpu_idle_reason()
    monkeypatch.setattr(backend, 'REFERENCE_DECOMPOSE_ACTIVE', 0)
    queue['queue_pending'] = [['external']]
    assert '队列' in backend.gpu_idle_reason()
    queue['queue_pending'] = []
    assert backend.gpu_idle_reason() is None
    queue.clear()
    with pytest.raises(ValueError, match='不完整'):
        backend.gpu_idle_reason()


def test_history_token_requires_one_valid_receipt(monkeypatch):
    history = {'prompt-a': {'status': {'completed': True}}}
    monkeypatch.setattr(backend, 'request_json', lambda path: history)
    assert backend.comfy_history_token() == 'prompt-a'
    history.clear()
    history['error'] = 'bad response'
    with pytest.raises(ValueError, match='不完整'):
        backend.comfy_history_token()


def test_release_final_check_and_submission_share_admission_lock(monkeypatch):
    now = [0.0]
    entered = threading.Event()
    finish = threading.Event()
    admitted = threading.Event()
    events = []
    lock = backend.GPU_ADMISSION_LOCK
    def release():
        events.append('release-start')
        entered.set()
        assert finish.wait(3)
        events.append('release-end')
        return {}
    controller = IdleGPURelease(lock, lambda: None, release, lambda: None,
                                grace_seconds=1, clock=lambda: now[0])
    monkeypatch.setattr(backend, 'GPU_IDLE_RELEASER', controller)
    now[0] = 1
    releasing = threading.Thread(target=controller.poll)
    releasing.start()
    assert entered.wait(3)
    @backend.gpu_admission
    def submit():
        events.append('admitted')
        admitted.set()
    submitting = threading.Thread(target=submit)
    submitting.start()
    assert not admitted.wait(0.05)
    finish.set()
    releasing.join(3)
    submitting.join(3)
    assert events == ['release-start', 'release-end', 'admitted']
    assert admitted.is_set() and not controller.released
