"""Single-worker, fail-closed idle release for ComfyUI's shared model cache."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any


# The production entry point is Uvicorn; its configured error logger writes
# INFO diagnostics to the supervised stderr log, unlike the unconfigured root.
log = logging.getLogger("uvicorn.error")


class IdleGPURelease:
    def __init__(
        self,
        admission_lock: threading.RLock,
        inspect: Callable[[], str | None],
        release: Callable[[], dict[str, Any]],
        sample_free_mib: Callable[[], int | None],
        *,
        grace_seconds: float = 120,
        poll_seconds: float = 15,
        retry_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        activity_token: Callable[[], str] | None = None,
    ) -> None:
        self.admission_lock = admission_lock
        self.inspect = inspect
        self.release = release
        self.sample_free_mib = sample_free_mib
        self.grace_seconds = grace_seconds
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.activity_token = activity_token
        self.last_activity_token: str | None = None
        self.idle_since = clock()
        self.retry_after = 0.0
        self.released = False
        self.state = "waiting"
        self.last_result: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self.admission_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.idle_since = self.clock()
            self.released = False
            self.state = "waiting"
            self.last_activity_token = None
            self._thread = threading.Thread(target=self._run, name="director-gpu-idle-release", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.poll()

    def note_activity(self) -> None:
        with self.admission_lock:
            self.idle_since = self.clock()
            self.retry_after = 0.0
            self.released = False
            self.state = "waiting"

    def note_manual_release(self) -> None:
        with self.admission_lock:
            self.released = True
            self.state = "released"

    def poll(self) -> None:
        # This lock also covers every workbench GPU admission, including
        # submission/resume and reference analysis. The final queue read and
        # /free request are one admission-critical section.
        with self.admission_lock:
            now = self.clock()
            try:
                if self.activity_token is not None:
                    token = self.activity_token()
                    if self.last_activity_token is None:
                        # First observation is a baseline, not proof that the
                        # service has been idle since process startup.
                        self.idle_since = now
                    elif token != self.last_activity_token:
                        log.info("ComfyUI history changed; restarting GPU idle grace period")
                        self.idle_since = now
                        self.retry_after = 0.0
                        self.released = False
                    self.last_activity_token = token
                busy_reason = self.inspect()
            except Exception as exc:
                if not self.released:
                    self.idle_since = now
                if self.state != "uncertain":
                    log.warning("GPU idle release postponed: state check failed: %s", exc)
                self.state = "uncertain"
                return
            if busy_reason:
                if self.state != "busy":
                    log.info("GPU idle release postponed: %s", busy_reason)
                self.idle_since = now
                self.retry_after = 0.0
                self.released = False
                self.state = "busy"
                return
            if self.released:
                self.state = "released"
                return
            if now - self.idle_since < self.grace_seconds or now < self.retry_after:
                self.state = "waiting"
                return
            try:
                before = self._sample()
                receipt = self.release()
            except Exception as exc:
                self.retry_after = now + self.retry_seconds
                self.state = "retry"
                log.warning("GPU idle release request failed; retry after %.0fs: %s", self.retry_seconds, exc)
                return
            self.released = True  # HTTP acceptance, not proof that VRAM already fell.
            self.state = "released"
            after = self._sample()
            self.last_result = {"requested_at": time.time(), "gpu_free_mib_before": before,
                                "gpu_free_mib_after_request": after, "comfy_receipt": receipt}
            log.info("GPU idle /free accepted after %.0fs idle; free MiB before=%s after_request=%s receipt=%s",
                     now - self.idle_since, before, after, receipt)

    def _sample(self) -> int | None:
        try:
            return self.sample_free_mib()
        except Exception as exc:
            log.warning("GPU idle release memory sample unavailable: %s", exc)
            return None
