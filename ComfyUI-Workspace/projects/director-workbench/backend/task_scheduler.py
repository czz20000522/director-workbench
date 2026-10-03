"""Persistent single-GPU admission and bounded CPU dispatch.

The app owns validation, authorization, frozen requests and execution receipts.
This module only claims existing task rows; it never submits directly to ComfyUI.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from typing import Any, Callable

log = logging.getLogger("uvicorn.error")
WAITING = {"scheduler_waiting", "batch_waiting"}
TERMINAL = {"succeeded", "failed", "stopped"}
CPU_KINDS = {"audio_edit", "media_operation"}
EXECUTING = {"preparing", "submitting", "queued", "running", "submitted", "stopping", "stop_requested", "finalizing", "needs_reconcile"}
REASON_MESSAGES = {
    "waiting_execution_slot": "等待共享执行资源", "waiting_reconciliation": "有执行回执待核对",
    "waiting_dependency": "等待前置任务完成", "dependency_unresolved": "前置任务回执待核对",
    "dependency_missing": "前置任务不存在", "dependency_failed": "前置任务失败或已停止",
    "resource_status_unknown": "资源状态暂时无法确认", "dispatching": "正在派发",
}


def normalize_reason(reason) -> dict[str, str]:
    if isinstance(reason, dict):
        code = str(reason.get("code") or "resource_status_unknown")
        return {"code": code, "message": str(reason.get("message") or REASON_MESSAGES.get(code, "等待资源或补齐条件"))}
    code = str(reason)
    return {"code": code, "message": REASON_MESSAGES.get(code, "等待资源或补齐条件")}


def owner(task: dict[str, Any]) -> str:
    return str(task["payload"].get("owner_user") or "__legacy__")


def resource(task: dict[str, Any]) -> str:
    return "cpu" if task["payload"].get("kind") in CPU_KINDS else "gpu"


class QueueLimitError(ValueError):
    def __init__(self, code: str, limit: int):
        self.detail = {"code": code, "limit": limit, "scope": "global" if code == "queue_global_limit" else "user",
                       "message": "待办任务已达上限，请等待或取消自己的未派发任务", "retryable": True,
                       "action": "wait_or_cancel_pending"}
        super().__init__(code)


class TaskScheduler:
    def __init__(self, connect: Callable, db_lock, admission_lock,
                 resource_reason: Callable[[], dict | str | None],
                 revalidate: Callable[[dict[str, Any]], dict | str | None],
                 execute: Callable[[dict[str, Any]], None],
                 note_activity: Callable[[], None] = lambda: None,
                 completed: Callable[[dict[str, Any]], None] = lambda task: None,
                 *, cpu_workers: int = 2, poll_seconds: float = 1,
                 global_limit: int = 256, user_limit: int = 64,
                 clock: Callable[[], float] = time.time):
        self.connect, self.db_lock, self.admission_lock = connect, db_lock, admission_lock
        self.resource_reason, self.revalidate, self.execute = resource_reason, revalidate, execute
        self.note_activity, self.completed = note_activity, completed
        self.cpu_workers = max(1, int(cpu_workers))
        self.poll_seconds = max(.05, float(poll_seconds))
        self.global_limit, self.user_limit = int(global_limit), int(user_limit)
        self.clock = clock
        self._active: dict[str, str] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_factory = threading.Thread

    def _rows(self, connection) -> list[dict[str, Any]]:
        # Batch rows share a timestamp and UUIDs have no sequence meaning.
        # SQLite insertion order retains FIFO across those timestamp ties.
        rows = connection.execute("SELECT * FROM tasks ORDER BY created_at,rowid").fetchall()
        result = []
        for row in rows:
            task = dict(row)
            task["payload"] = json.loads(task["payload"])
            if task.get("result"):
                task["result"] = json.loads(task["result"])
            result.append(task)
        return result

    def _meta(self, connection) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS scheduler_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _set_meta(self, connection, key: str, value: str) -> None:
        self._meta(connection)
        connection.execute("INSERT INTO scheduler_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value", (key, value))

    def _last_owner(self, connection, kind: str) -> str | None:
        self._meta(connection)
        row = connection.execute("SELECT value FROM scheduler_meta WHERE key=?", ("last_" + kind + "_user",)).fetchone()
        return row[0] if row else None

    def check_limits(self, connection, owner_user: str, count: int = 1) -> None:
        """Call under the app admission/DB lock in the same transaction as inserts."""
        pending = [task for task in self._rows(connection) if task["status"] not in TERMINAL]
        if len(pending) + count > self.global_limit:
            raise QueueLimitError("queue_global_limit", self.global_limit)
        if sum(owner(task) == (owner_user or "__legacy__") for task in pending) + count > self.user_limit:
            raise QueueLimitError("queue_user_limit", self.user_limit)

    def recover(self) -> int:
        """Only at startup: ambiguous execution requires existing receipt reconciliation."""
        with self.admission_lock, self.db_lock, self.connect() as connection:
            self._meta(connection)
            tasks = self._rows(connection)
            count = 0
            for task in tasks:
                if task["status"] in WAITING and task.get("stop_requested"):
                    connection.execute("UPDATE tasks SET status='stopped',updated_at=?,error=? WHERE id=?", (self.clock(), "Cancelled before dispatch", task["id"]))
                elif task["status"] not in WAITING | TERMINAL | {"needs_reconcile"}:
                    connection.execute("UPDATE tasks SET status='needs_reconcile',updated_at=?,error=? WHERE id=?", (self.clock(), "Service restarted; reconcile the original execution receipt before retrying", task["id"]))
                    count += 1
            connection.commit()
            return count

    def start(self) -> None:
        with self.admission_lock:
            if self._thread and self._thread.is_alive():
                return
            self.recover()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="task-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        # Executors retain their slots and receipts until they actually finish.

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Task scheduler poll failed; queued requests retained")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def cancel_pending(self, task_id: str) -> bool:
        """Authorization is enforced by the public task-control route."""
        with self.admission_lock, self.db_lock, self.connect() as connection:
            count = connection.execute("UPDATE tasks SET status='stopped',stop_requested=1,updated_at=?,error=? WHERE id=? AND status IN ('scheduler_waiting','batch_waiting')", (self.clock(), "Cancelled before dispatch", task_id)).rowcount
            connection.commit()
        self.wake()
        return bool(count)

    def _dependency_reason(self, task, tasks) -> tuple[str | None, bool]:
        by_id = {row["id"]: row for row in tasks}
        predecessors = list(task["payload"].get("scheduler_dependencies") or [])
        if task.get("batch_id"):
            predecessors += [row["id"] for row in tasks if row.get("batch_id") == task["batch_id"] and (row.get("sequence") or 0) < (task.get("sequence") or 0)]
        for predecessor in predecessors:
            previous = by_id.get(predecessor)
            if not previous:
                return "dependency_missing", False
            if previous["status"] in {"failed", "stopped"}:
                return "dependency_failed", True
            if previous["status"] != "succeeded":
                return "dependency_unresolved" if previous["status"] == "needs_reconcile" else "waiting_dependency", False
        return None, False

    def _ordered(self, connection, tasks, kind):
        groups: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            if task["status"] in WAITING and resource(task) == kind:
                groups.setdefault(owner(task), []).append(task)
        previous = self._last_owner(connection, kind)
        # Keep the ring stable as each user's oldest remaining row changes.
        # Retain the last user's anchor until another user is dispatched, even
        # when that user's final task has just finished.
        row = connection.execute("SELECT value FROM scheduler_meta WHERE key=?", (kind + "_user_ring",)).fetchone()
        old_ring = json.loads(row[0]) if row else []
        current = {owner(task) for task in tasks if resource(task) == kind and task["status"] not in TERMINAL}
        ring = [user for user in old_ring if user in current or user == previous]
        for task in tasks:
            if resource(task) == kind and task["status"] not in TERMINAL and owner(task) not in ring:
                ring.append(owner(task))
        self._set_meta(connection, kind + "_user_ring", json.dumps(ring))
        users = ring
        if previous in users:
            index = users.index(previous)
            users = users[index + 1:] + users[:index + 1]
        users = [user for user in users if user in groups]
        ordered = []
        while users:
            for user in list(users):
                ordered.append(groups[user].pop(0))
                if not groups[user]:
                    users.remove(user)
        return ordered

    def tick(self, *, threaded: bool = True) -> list[str]:
        claimed = []
        with self.admission_lock:
            with self.db_lock, self.connect() as connection:
                self._meta(connection)
                tasks = self._rows(connection)
                last = {kind: self._ordered(connection, tasks, kind) for kind in ("gpu", "cpu")}
                connection.commit()
            for kind in ("gpu", "cpu"):
                occupied_ids = {task["id"] for task in tasks if resource(task) == kind and task["status"] not in WAITING | TERMINAL}
                occupied_ids.update(task_id for task_id, value in self._active.items() if value == kind)
                slots = (1 if kind == "gpu" else self.cpu_workers) - len(occupied_ids)
                if slots <= 0:
                    self._record_wait(last[kind], "waiting_reconciliation" if any(task["status"] == "needs_reconcile" and resource(task) == kind for task in tasks) else "waiting_execution_slot")
                    continue
                if kind == "gpu" and last[kind]:
                    try:
                        reason = self.resource_reason()
                    except Exception:
                        reason = "resource_status_unknown"
                    if reason:
                        self._record_wait(last[kind], reason)
                        continue
                seen = set()
                for task in last[kind]:
                    if slots <= 0:
                        break
                    user = owner(task)
                    if user in seen:
                        continue  # Preserve each user's FIFO even when their head is blocked.
                    seen.add(user)
                    reason, stop = self._dependency_reason(task, tasks)
                    if reason:
                        self._record_wait([task], reason, stop=stop)
                        continue
                    try:
                        reason = self.revalidate(task)
                    except Exception as exc:
                        self._fail(task["id"], str(getattr(exc, "detail", None) or exc))
                        continue
                    if reason:
                        self._record_wait([task], reason)
                        continue
                    with self.db_lock, self.connect() as connection:
                        count = connection.execute("UPDATE tasks SET status='preparing',updated_at=?,error=NULL WHERE id=? AND status IN ('scheduler_waiting','batch_waiting') AND stop_requested=0", (self.clock(), task["id"])).rowcount
                        if count:
                            self._set_meta(connection, "last_" + kind + "_user", user)
                            self._set_meta(connection, "reason:" + task["id"], "dispatching")
                        connection.commit()
                    if not count:
                        continue
                    task["status"] = "preparing"
                    self._active[task["id"]] = kind
                    if kind == "gpu":
                        self.note_activity()
                    claimed.append(task)
                    slots -= 1
            for task in claimed:
                if threaded:
                    runner = None
                    try:
                        runner = self._thread_factory(target=self._run, args=(task,), name="task-" + task["id"], daemon=True)
                        runner.start()
                    except Exception as exc:
                        if runner is None or runner.ident is None:
                            # No executor thread started, so no submission is possible.
                            self._active.pop(task["id"], None)
                            self._fail(task["id"], "Executor could not start: " + str(exc))
                            self.wake()
                        else:
                            # An unusual start failure after actual launch must retain
                            # the slot; the executor's receipt still controls completion.
                            log.exception("Executor thread startup uncertain for %s", task["id"])
        if not threaded:
            for task in claimed:
                self._run(task)
        return [task["id"] for task in claimed]

    def _record_wait(self, tasks, reason, *, stop=False):
        structured = normalize_reason(reason)
        with self.db_lock, self.connect() as connection:
            for task in tasks:
                self._set_meta(connection, "reason:" + task["id"], json.dumps(structured, ensure_ascii=False))
                if stop:
                    connection.execute("UPDATE tasks SET status='stopped',updated_at=?,error=? WHERE id=? AND status IN ('scheduler_waiting','batch_waiting')", (self.clock(), structured["code"], task["id"]))
            connection.commit()

    def _fail(self, task_id, error):
        with self.db_lock, self.connect() as connection:
            connection.execute("UPDATE tasks SET status='failed',updated_at=?,error=? WHERE id=? AND status IN ('preparing','scheduler_waiting','batch_waiting')", (self.clock(), error, task_id))
            connection.commit()

    def _run(self, task):
        try:
            self.execute(task)
        except Exception as exc:
            # The runner may have submitted before raising. Never assume it did not.
            with self.db_lock, self.connect() as connection:
                connection.execute("UPDATE tasks SET status='needs_reconcile',updated_at=?,error=? WHERE id=? AND status NOT IN ('succeeded','failed','stopped')", (self.clock(), "Execution receipt uncertain: " + str(exc), task["id"]))
                connection.commit()
        finally:
            with self.admission_lock:
                with self.db_lock, self.connect() as connection:
                    current = next((row for row in self._rows(connection) if row["id"] == task["id"]), task)
                    if current["status"] not in TERMINAL | {"needs_reconcile"}:
                        connection.execute("UPDATE tasks SET status='needs_reconcile',updated_at=?,error=? WHERE id=?", (self.clock(), "Executor exited without a final receipt", task["id"]))
                        current["status"] = "needs_reconcile"
                    connection.commit()
                self._active.pop(task["id"], None)
            try:
                self.completed(current)
            except Exception:
                log.exception("Task scheduler completion hook failed for %s", task["id"])
            self.wake()

    def queue_info(self, task_id: str) -> dict[str, Any]:
        with self.admission_lock, self.db_lock, self.connect() as connection:
            tasks = self._rows(connection)
            task = next((row for row in tasks if row["id"] == task_id), None)
            if not task:
                return {}
            queued = task["status"] in WAITING
            ordered = self._ordered(connection, tasks, resource(task))
            row = connection.execute("SELECT value FROM scheduler_meta WHERE key=?", ("reason:" + task_id,)).fetchone()
            connection.commit()
            dependency_reason, _ = self._dependency_reason(task, tasks)
            if dependency_reason:
                reason = normalize_reason(dependency_reason)
            elif row:
                try:
                    reason = normalize_reason(json.loads(row[0]))
                except (ValueError, TypeError):
                    reason = normalize_reason(row[0])
            else:
                reason = normalize_reason("waiting_execution_slot")
            if queued:
                state = "waiting_dependency" if reason["code"] in {"waiting_dependency", "dependency_unresolved", "dependency_missing", "dependency_failed"} else "waiting_resource"
            else:
                state = "needs_reconcile" if task["status"] == "needs_reconcile" else "terminal" if task["status"] in TERMINAL else "executing"
            return {"state": state, "reason": reason if queued else None,
                    "queued": queued, "resource": resource(task),
                    "position": next((i + 1 for i, item in enumerate(ordered) if item["id"] == task_id), None) if queued else None,
                    "reason_code": reason["code"] if queued else None,
                    "global_limit": self.global_limit, "user_limit": self.user_limit}

    def summary(self) -> dict[str, Any]:
        with self.admission_lock, self.db_lock, self.connect() as connection:
            tasks = self._rows(connection)
        return {"status_counts": dict(Counter(task["status"] for task in tasks)),
                "waiting_gpu": sum(task["status"] in WAITING and resource(task) == "gpu" for task in tasks),
                "waiting_cpu": sum(task["status"] in WAITING and resource(task) == "cpu" for task in tasks),
                "gpu_slots": 1, "cpu_slots": self.cpu_workers,
                "global_limit": self.global_limit, "user_limit": self.user_limit}

    def runnable_waiting(self) -> bool:
        """Ready GPU heads block idle release; input/dependency holds do not.

        Resource state is checked by the app's idle-release guard separately,
        avoiding a recursive resource callback. This only validates saved input.
        """
        with self.admission_lock:
            with self.db_lock, self.connect() as connection:
                tasks = self._rows(connection)
            seen = set()
            for task in tasks:
                if task["status"] not in WAITING or resource(task) != "gpu" or task.get("stop_requested"):
                    continue
                user = owner(task)
                if user in seen:
                    continue
                seen.add(user)
                if self._dependency_reason(task, tasks)[0]:
                    continue
                try:
                    if not self.revalidate(task):
                        return True
                except Exception:
                    continue
            return False
