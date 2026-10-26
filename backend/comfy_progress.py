"""真实步级进度：订阅 ComfyUI 的 ``progress_state`` 事件。

ComfyUI 0.42.1 把进度改成按节点的 ``progress_state``，而且只回给**提交任务时用的那个
client_id**（``comfy_execution/progress.py`` 的 WebUIProgressHandler 把 sid 固定成
``server_instance.client_id``）。HTTP 的 /queue 里没有步数。所以工作台必须用提交方同一个
clientId 去连 ``/ws``，否则什么都收不到。

本工作台有两条真实提交路径，各自的身份不同，因此每个身份各开一条监听：
``director-workbench-assembly-v2`` 来自 backend/app.py 的 run_workflow_submission，
``aigc-video-production`` 来自 .agents/skills/aigc-video-production/scripts/h3_render.py。
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any, AsyncIterator, Iterator

from websockets.sync.client import connect

CLIENT_WORKBENCH = 'director-workbench-assembly-v2'
CLIENT_RENDERER = 'aigc-video-production'
CLIENT_IDS = (CLIENT_WORKBENCH, CLIENT_RENDERER)
STALE_SECONDS = 180.0
MAX_TRACKED = 64


class ProgressBoard:
    """Latest ComfyUI progress per prompt_id, with the timing needed for an honest ETA."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._connected: dict[str, bool] = {client_id: False for client_id in CLIENT_IDS}

    def mark(self, client_id: str, connected: bool) -> None:
        with self._lock:
            self._connected[client_id] = connected

    def record(self, prompt_id: str, nodes: dict[str, Any], client_id: str, at: float) -> None:
        best = _active_node(nodes)
        with self._lock:
            if best is None:
                # 没有任何在跑的节点：要么整条已完成，要么还没开始。保留已有记录，
                # 让任务状态本身去收尾，进度只做展示。
                return
            previous = self._entries.get(prompt_id)
            value = float(best.get('value') or 0)
            maximum = float(best.get('max') or 1) or 1
            same_node = previous and previous.get('node_id') == best.get('node_id')
            started = previous['started_at'] if same_node else at
            anchor = previous['anchor_value'] if same_node else value
            # 以第一个样本的 (时间, 步数) 为锚点，用增量算每步耗时：把首样本当成第 0 步
            # 会低估，进度条就会在真正跑完前冲到 100%。
            elapsed, advanced = at - started, value - anchor
            per_step = (elapsed / advanced) if advanced > 0 else None
            entry = {
                'prompt_id': prompt_id, 'node_id': str(best.get('node_id') or ''),
                'state': str(best.get('state') or 'running'), 'value': value, 'max': maximum,
                'percent': min(100, int(value / maximum * 100)), 'client_id': client_id,
                'started_at': started, 'anchor_value': anchor, 'updated_at': at,
                'seconds_per_step': round(per_step, 3) if per_step else None,
                'eta_seconds': round((maximum - value) * per_step) if per_step and maximum > value else None,
            }
            self._entries[prompt_id] = entry
            if len(self._entries) > MAX_TRACKED:
                for stale in sorted(self._entries, key=lambda key: self._entries[key]['updated_at'])[:-MAX_TRACKED]:
                    del self._entries[stale]

    def for_prompt(self, prompt_id: str | None) -> dict[str, Any] | None:
        if not prompt_id:
            return None
        now = time.time()
        with self._lock:
            entry = self._entries.get(str(prompt_id))
            if not entry:
                return None
            if now - entry['updated_at'] > STALE_SECONDS:
                del self._entries[str(prompt_id)]
                return None
            return dict(entry)

    def forget(self, prompt_id: str) -> None:
        with self._lock:
            self._entries.pop(str(prompt_id), None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'listeners': dict(self._connected), 'tracked': len(self._entries)}


def visible_progress(task: dict[str, Any]) -> dict[str, Any] | None:
    """进度只在任务还在跑的时候有意义。

    完成后最后一条采样会僵在进度条上（常常停在解码那一步的 0/1），看起来像还在生成，
    所以终态一律不报进度；任务状态本身才是结论。
    """
    if str(task.get('status') or '') in {'succeeded', 'failed', 'stopped'}:
        return None
    return BOARD.for_prompt(task.get('prompt_id'))


def _active_node(nodes: dict[str, Any]) -> dict[str, Any] | None:
    running = [item for item in (nodes or {}).values()
               if isinstance(item, dict) and item.get('state') == 'running']
    if not running:
        return None
    # 采样节点步数最多，最能代表"这一镜跑到哪"；编码/解码类节点只有一两步。
    return max(running, key=lambda item: float(item.get('max') or 1))


BOARD = ProgressBoard()


def _handle(message: Any, client_id: str) -> None:
    if isinstance(message, (bytes, bytearray)):
        return
    try:
        event = json.loads(message)
    except ValueError:
        return
    if event.get('type') != 'progress_state':
        return
    data = event.get('data') or {}
    prompt_id = str(data.get('prompt_id') or '')
    if prompt_id:
        BOARD.record(prompt_id, data.get('nodes') or {}, client_id, time.time())


def socket_url(base_url: str) -> str:
    """COMFY_URL is an http(s) origin; the progress stream needs the ws scheme."""
    url = base_url.rstrip('/')
    for scheme, replacement in (('https://', 'wss://'), ('http://', 'ws://')):
        if url.startswith(scheme):
            return replacement + url[len(scheme):]
    return url


def _listen(client_id: str, base_url: str, stop: threading.Event) -> None:
    url = f"{socket_url(base_url)}/ws?clientId={client_id}"
    while not stop.is_set():
        try:
            with connect(url, max_size=8 * 1024 * 1024, close_timeout=2, open_timeout=5) as socket:
                BOARD.mark(client_id, True)
                while not stop.is_set():
                    try:
                        _handle(socket.recv(timeout=2.0), client_id)
                    except TimeoutError:
                        continue
        except OSError:
            pass
        except Exception:
            # ComfyUI 重启、连接被掐断都属于常态；下一轮重连即可。
            pass
        finally:
            BOARD.mark(client_id, False)
        stop.wait(2.0)


@contextlib.contextmanager
def listener(base_url: str) -> Iterator[dict[str, Any]]:
    """One daemon thread per submission identity, stopped when the workbench shuts down."""
    stop = threading.Event()
    threads = [threading.Thread(target=_listen, args=(client_id, base_url, stop), daemon=True,
                                name=f'comfy-progress-{client_id}') for client_id in CLIENT_IDS]
    for thread in threads:
        thread.start()
    try:
        yield {'started': [thread.name for thread in threads]}
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=3)


@contextlib.asynccontextmanager
async def lifespan(base_url: str) -> AsyncIterator[None]:
    """The async form the workbench's lifespan subsystems expect."""
    with listener(base_url):
        yield
