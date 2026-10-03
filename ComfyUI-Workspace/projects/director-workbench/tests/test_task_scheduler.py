import json
import sqlite3
import threading
import time

import pytest

from backend.task_scheduler import QueueLimitError, TaskScheduler


@pytest.fixture
def scheduler_db(tmp_path):
    path = tmp_path / "isolated-tasks.sqlite3"

    def connect():
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    with connect() as connection:
        connection.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL,
            prompt_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            stop_requested INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            result TEXT, error TEXT, batch_id TEXT, sequence INTEGER, project_id TEXT
        )""")
    return connect


def put(connect, task_id, user="A", *, status="scheduler_waiting", batch=None,
        sequence=None, kind=None, dependencies=None, stop=False):
    payload = {"owner_user": user, "private_input": "secret-server-path"}
    if kind:
        payload["kind"] = kind
    if dependencies:
        payload["scheduler_dependencies"] = dependencies
    with connect() as connection:
        created = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        connection.execute("INSERT INTO tasks(id,asset_id,status,created_at,updated_at,payload,batch_id,sequence,project_id,stop_requested) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (task_id, "S01", status, created, created, json.dumps(payload), batch, sequence, "private-" + user, int(stop)))


def state(connect, task_id):
    with connect() as connection:
        return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def finish(connect, task_id, status="succeeded"):
    with connect() as connection:
        connection.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def make(connect, **kwargs):
    executed = []

    def execute(task):
        executed.append(task["id"])
        finish(connect, task["id"])

    scheduler = TaskScheduler(connect, threading.Lock(), threading.RLock(),
                              kwargs.pop("resource_reason", lambda: None),
                              kwargs.pop("revalidate", lambda task: None),
                              kwargs.pop("execute", execute), **kwargs)
    return scheduler, executed


def test_fair_batch_round_robin_and_persisted_turn(scheduler_db):
    for i in range(1, 4):
        put(scheduler_db, "A" + str(i), batch="a", sequence=i, status="batch_waiting")
    for i in range(1, 3):
        put(scheduler_db, "B" + str(i), "B", batch="b", sequence=i, status="batch_waiting")
    scheduler, executed = make(scheduler_db)
    assert scheduler.tick(threaded=False) == ["A1"]
    # Recreating the scheduler does not reset the last dispatched account.
    scheduler, rest = make(scheduler_db)
    for _ in range(4):
        scheduler.tick(threaded=False)
    assert executed + rest == ["A1", "B1", "A2", "B2", "A3"]
    assert scheduler.tick(threaded=False) == []


def test_new_user_during_running_batch_gets_next_boundary(scheduler_db):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    executed = []

    def execute(task):
        executed.append(task["id"])
        if task["id"] == "A1":
            entered.set()
            assert release.wait(3)
        finish(scheduler_db, task["id"])

    scheduler, _ = make(scheduler_db, execute=execute, completed=lambda task: completed.set())
    put(scheduler_db, "A1", batch="a", sequence=1)
    put(scheduler_db, "A2", batch="a", sequence=2)
    scheduler.tick()
    assert entered.wait(3)
    put(scheduler_db, "B1", "B")
    assert scheduler.tick(threaded=False) == []
    release.set()
    assert completed.wait(3)
    assert scheduler.tick(threaded=False) == ["B1"]
    assert scheduler.tick(threaded=False) == ["A2"]
    assert executed == ["A1", "B1", "A2"]


def test_external_queue_or_unknown_status_retains_requests(scheduler_db):
    busy = [{"code": "comfy_external_busy", "message": "外部任务正在运行"}]
    scheduler, executed = make(scheduler_db, resource_reason=lambda: busy[0])
    put(scheduler_db, "A1")
    assert scheduler.tick(threaded=False) == []
    info = scheduler.queue_info("A1")
    assert info["state"] == "waiting_resource"
    assert info["reason"] == busy[0]
    assert info["position"] == 1
    busy[0] = None
    assert scheduler.tick(threaded=False) == ["A1"]
    assert executed == ["A1"]


def test_resource_query_failure_cannot_dispatch(scheduler_db):
    def unavailable():
        raise OSError("offline")
    scheduler, executed = make(scheduler_db, resource_reason=unavailable)
    put(scheduler_db, "A1")
    assert scheduler.tick(threaded=False) == []
    assert scheduler.queue_info("A1")["reason_code"] == "resource_status_unknown"
    assert executed == []


def test_cancel_unstarted_batch_head_stops_successors(scheduler_db):
    put(scheduler_db, "A1", batch="a", sequence=1)
    put(scheduler_db, "A2", batch="a", sequence=2)
    scheduler, executed = make(scheduler_db)
    assert scheduler.cancel_pending("A1")
    assert not scheduler.cancel_pending("A1")
    scheduler.tick(threaded=False)
    assert executed == []
    assert state(scheduler_db, "A2")["status"] == "stopped"


@pytest.mark.parametrize("execution_status", ["preparing", "submitting", "queued", "running", "stopping", "stop_requested"])
def test_restart_retains_waiting_and_holds_unknown_gpu_receipt(scheduler_db, execution_status):
    put(scheduler_db, "A1", status=execution_status)
    put(scheduler_db, "B1", "B")
    scheduler, executed = make(scheduler_db)
    assert scheduler.recover() == 1
    assert scheduler.recover() == 0
    assert state(scheduler_db, "A1")["status"] == "needs_reconcile"
    assert state(scheduler_db, "B1")["status"] == "scheduler_waiting"
    assert scheduler.tick(threaded=False) == []
    assert scheduler.queue_info("B1")["reason_code"] == "waiting_reconciliation"
    # Only a positively reconciled original receipt can unlock new work.
    finish(scheduler_db, "A1")
    assert scheduler.tick(threaded=False) == ["B1"]
    assert executed == ["B1"]


def test_unknown_executor_receipt_is_never_replayed(scheduler_db):
    put(scheduler_db, "A1")
    put(scheduler_db, "B1", "B")
    calls = []
    def execute(task):
        calls.append(task["id"])
        raise TimeoutError("ComfyUI accepted but response unknown")
    scheduler, _ = make(scheduler_db, execute=execute)
    scheduler.tick(threaded=False)
    assert state(scheduler_db, "A1")["status"] == "needs_reconcile"
    assert scheduler.tick(threaded=False) == []
    assert calls == ["A1"]


def test_executor_without_terminal_receipt_is_held(scheduler_db):
    put(scheduler_db, "A1")
    scheduler, _ = make(scheduler_db, execute=lambda task: None)
    scheduler.tick(threaded=False)
    assert state(scheduler_db, "A1")["status"] == "needs_reconcile"
    assert scheduler.queue_info("A1")["state"] == "needs_reconcile"


def test_failed_batch_stops_later_segments_and_other_user_runs(scheduler_db):
    put(scheduler_db, "A1", batch="a", sequence=1, status="failed")
    put(scheduler_db, "A2", batch="a", sequence=2)
    put(scheduler_db, "A3", batch="a", sequence=3)
    put(scheduler_db, "B1", "B")
    scheduler, executed = make(scheduler_db)
    assert scheduler.tick(threaded=False) == ["B1"]
    scheduler.tick(threaded=False)
    assert state(scheduler_db, "A2")["status"] == "stopped"
    assert state(scheduler_db, "A3")["status"] == "stopped"
    assert executed == ["B1"]


def test_blocked_user_head_does_not_starve_other_user_or_reorder_own_tasks(scheduler_db):
    put(scheduler_db, "A1", dependencies=["missing"])
    put(scheduler_db, "A2")
    put(scheduler_db, "B1", "B")
    scheduler, executed = make(scheduler_db)
    assert scheduler.tick(threaded=False) == ["B1"]
    assert scheduler.tick(threaded=False) == []
    assert scheduler.queue_info("A1")["state"] == "waiting_dependency"
    assert executed == ["B1"]


def test_dispatch_revalidates_frozen_material_and_permanent_errors(scheduler_db):
    put(scheduler_db, "A1")
    put(scheduler_db, "B1", "B")
    def revalidate(task):
        if task["id"] == "A1":
            raise ValueError("Frozen input is no longer readable")
    scheduler, executed = make(scheduler_db, revalidate=revalidate)
    assert scheduler.tick(threaded=False) == ["B1"]
    assert state(scheduler_db, "A1")["status"] == "failed"
    assert executed == ["B1"]


def test_running_stop_request_keeps_slot_until_executor_confirms(scheduler_db):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    put(scheduler_db, "A1")
    put(scheduler_db, "B1", "B")
    def execute(task):
        if task["id"] == "A1":
            entered.set()
            assert release.wait(3)
            finish(scheduler_db, task["id"], "stopped")
        else:
            finish(scheduler_db, task["id"])
    scheduler, _ = make(scheduler_db, execute=execute, completed=lambda task: completed.set())
    scheduler.tick()
    assert entered.wait(3)
    assert not scheduler.cancel_pending("A1")
    finish(scheduler_db, "A1", "stop_requested")
    assert scheduler.tick(threaded=False) == []
    release.set()
    assert completed.wait(3)
    assert scheduler.tick(threaded=False) == ["B1"]


def test_cpu_parallel_pool_is_bounded_and_independent_of_gpu(scheduler_db):
    put(scheduler_db, "G1", status="needs_reconcile")
    for user in ("A", "B", "C"):
        put(scheduler_db, user + "cpu", user, kind="audio_edit")
    entered = {user: threading.Event() for user in ("A", "B", "C")}
    release = threading.Event()
    complete = threading.Event()
    done = []
    def execute(task):
        entered[task["payload"]["owner_user"]].set()
        assert release.wait(3)
        finish(scheduler_db, task["id"])
    def completed(task):
        done.append(task["id"])
        if len(done) == 2:
            complete.set()
    scheduler, _ = make(scheduler_db, execute=execute, completed=completed, cpu_workers=2)
    assert scheduler.tick() == ["Acpu", "Bcpu"]
    assert entered["A"].wait(3) and entered["B"].wait(3)
    assert scheduler.tick(threaded=False) == []
    assert not entered["C"].is_set()
    release.set()
    assert complete.wait(3)
    assert scheduler.tick(threaded=False) == ["Ccpu"]
    assert state(scheduler_db, "G1")["status"] == "needs_reconcile"


def test_cpu_unknown_receipt_does_not_block_gpu(scheduler_db):
    put(scheduler_db, "A-cpu", status="needs_reconcile", kind="media_operation")
    put(scheduler_db, "B-gpu", "B")
    scheduler, executed = make(scheduler_db)
    assert scheduler.tick(threaded=False) == ["B-gpu"]
    assert executed == ["B-gpu"]


def test_capacity_counts_all_nonterminal_users_and_resources(scheduler_db):
    scheduler, _ = make(scheduler_db, global_limit=3, user_limit=2)
    put(scheduler_db, "A1", kind="audio_edit")
    put(scheduler_db, "A2", status="needs_reconcile")
    with scheduler.connect() as connection:
        with pytest.raises(QueueLimitError) as raised:
            scheduler.check_limits(connection, "A")
        assert raised.value.detail["code"] == "queue_user_limit"
        scheduler.check_limits(connection, "B")
    put(scheduler_db, "B1", "B")
    with scheduler.connect() as connection:
        with pytest.raises(QueueLimitError) as raised:
            scheduler.check_limits(connection, "C")
        assert raised.value.detail["code"] == "queue_global_limit"
    finish(scheduler_db, "A1", "stopped")
    with scheduler.connect() as connection:
        scheduler.check_limits(connection, "A")


def test_queue_details_and_aggregate_do_not_expose_other_users(scheduler_db):
    put(scheduler_db, "A1")
    put(scheduler_db, "B1", "B")
    scheduler, _ = make(scheduler_db)
    value = json.dumps({"own": scheduler.queue_info("A1"), "aggregate": scheduler.summary()})
    assert "private_input" not in value and "private-B" not in value and "B1" not in value
    assert scheduler.summary()["waiting_gpu"] == 2


def test_claim_and_idle_release_use_same_admission_lock(scheduler_db):
    put(scheduler_db, "A1")
    lock = threading.RLock()
    before_claim, unblock, completed = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def revalidate(task):
        before_claim.set()
        assert unblock.wait(3)
    scheduler, _ = make(scheduler_db, revalidate=revalidate, completed=lambda task: completed.set(), note_activity=lambda: seen.append("activity"))
    scheduler.admission_lock = lock
    polling = threading.Thread(target=scheduler.tick)
    polling.start()
    assert before_claim.wait(3)
    assert not lock.acquire(blocking=False)
    unblock.set()
    polling.join(3)
    assert not polling.is_alive()
    assert completed.wait(3)
    assert seen == ["activity"]


def test_explicit_lifecycle_repeated_start_no_duplicate_execution(scheduler_db):
    put(scheduler_db, "A1")
    done = threading.Event()
    scheduler, executed = make(scheduler_db, poll_seconds=.05, completed=lambda task: done.set())
    assert scheduler._thread is None
    scheduler.start()
    thread = scheduler._thread
    scheduler.start()
    assert scheduler._thread is thread
    assert done.wait(3)
    scheduler.stop()
    assert not thread.is_alive()
    assert executed == ["A1"]


def test_idle_readiness_ignores_cpu_and_dependency_holds(scheduler_db):
    scheduler, _ = make(scheduler_db)
    put(scheduler_db, "Acpu", kind="audio_edit")
    put(scheduler_db, "A1", dependencies=["unavailable"])
    put(scheduler_db, "A2")
    assert not scheduler.runnable_waiting()
    put(scheduler_db, "B1", "B")
    assert scheduler.runnable_waiting()
    assert scheduler.cancel_pending("B1")
    assert not scheduler.runnable_waiting()


def test_idle_readiness_material_hold_and_invalid_input_allow_free(scheduler_db):
    put(scheduler_db, "A1")
    valid = [False]
    scheduler, _ = make(scheduler_db, revalidate=lambda task: None if valid[0] else "waiting_material")
    assert not scheduler.runnable_waiting()
    valid[0] = True
    assert scheduler.runnable_waiting()
    scheduler.revalidate = lambda task: (_ for _ in ()).throw(ValueError("bad input"))
    assert not scheduler.runnable_waiting()


def test_unknown_status_is_conservatively_held_not_overlapped(scheduler_db):
    put(scheduler_db, "A1", status="future_inflight_status")
    put(scheduler_db, "B1", "B")
    scheduler, executed = make(scheduler_db)
    assert scheduler.tick(threaded=False) == []
    assert scheduler.recover() == 1
    assert state(scheduler_db, "A1")["status"] == "needs_reconcile"
    assert executed == []


def test_permanent_revalidate_failure_stops_batch_tails_on_following_poll(scheduler_db):
    for i in (1, 2, 3):
        put(scheduler_db, "A" + str(i), batch="a", sequence=i)
    put(scheduler_db, "B1", "B")
    def revalidate(task):
        if task["id"] == "A1":
            raise ValueError("Frozen material removed")
    scheduler, executed = make(scheduler_db, revalidate=revalidate)
    assert scheduler.tick(threaded=False) == ["B1"]
    scheduler.tick(threaded=False)
    scheduler.tick(threaded=False)
    assert [state(scheduler_db, "A" + str(i))["status"] for i in (1, 2, 3)] == ["failed", "stopped", "stopped"]
    assert executed == ["B1"]


def test_thread_start_failure_fails_without_holding_gpu_slot(scheduler_db):
    put(scheduler_db, "A1")
    put(scheduler_db, "B1", "B")
    scheduler, executed = make(scheduler_db)
    class NotStarted:
        ident = None
        def start(self):
            raise RuntimeError("Cannot allocate worker")
    scheduler._thread_factory = lambda **kwargs: NotStarted()
    scheduler.tick()
    assert state(scheduler_db, "A1")["status"] == "failed"
    assert scheduler._active == {}
    assert scheduler.tick(threaded=False) == ["B1"]
    assert executed == ["B1"]


def test_three_user_ring_survives_interleaved_created_times(scheduler_db):
    # B2 is newer than C1; completing B1 must not move B behind C in the ring
    # and cause the older A2 to jump ahead of C's first turn.
    for task_id, user in [("A1", "A"), ("A2", "A"), ("B1", "B"),
                          ("C1", "C"), ("B2", "B"), ("A3", "A"), ("C2", "C")]:
        put(scheduler_db, task_id, user)
    scheduler, executed = make(scheduler_db)
    for _ in range(7):
        scheduler.tick(threaded=False)
    assert executed == ["A1", "B1", "C1", "A2", "B2", "C2", "A3"]


def test_completed_user_ring_anchor_preserves_next_turn_after_restart(scheduler_db):
    for task_id, user in [("A1", "A"), ("A2", "A"), ("B1", "B"), ("C1", "C")]:
        put(scheduler_db, task_id, user)
    scheduler, first = make(scheduler_db)
    scheduler.tick(threaded=False)
    scheduler.tick(threaded=False)
    # B is now terminal, but C still owns the next turn, ahead of A2.
    scheduler, remaining = make(scheduler_db)
    scheduler.recover()
    scheduler.tick(threaded=False)
    scheduler.tick(threaded=False)
    assert first + remaining == ["A1", "B1", "C1", "A2"]


def test_equal_timestamp_batch_uuid_order_cannot_deadlock_user_fifo(scheduler_db):
    for sequence, task_id in enumerate(("z-first", "a-second", "m-third"), start=1):
        put(scheduler_db, task_id, batch="a", sequence=sequence)
    with scheduler_db() as connection:
        connection.execute("UPDATE tasks SET created_at=100")
    scheduler, executed = make(scheduler_db)
    for _ in range(3):
        scheduler.tick(threaded=False)
    assert executed == ["z-first", "a-second", "m-third"]
