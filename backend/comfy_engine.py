"""Start the local ComfyUI process on demand, before a generation submission.

The workbench is started at login; ComfyUI is not. An idle ComfyUI holds about
1 GB of RAM and 1 GB of VRAM, so it is only started when a task actually needs
it. This module is called from the submission runners, never from status polls.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .workbench_config import COMFY_ROOT, COMFY_URL, ROOT

LOGGER = logging.getLogger("uvicorn.error")
START_TIMEOUT_SECONDS = float(os.environ.get("DIRECTOR_COMFY_START_TIMEOUT_SECONDS", "240"))
LOCK = threading.Lock()


def online(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def launch_command() -> tuple[Path, list[str], Path]:
    home = Path(COMFY_ROOT) / "ComfyUI_windows_portable"
    python = home / "python_embeded" / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"找不到本地 ComfyUI：{python}")
    arguments = [
        "-s", "ComfyUI\\main.py",
        # --windows-standalone-build 默认会顺带在系统浏览器里打开 ComfyUI 页面；
        # 工作台自己在用 8188，不需要再弹一个网页出来。
        "--windows-standalone-build", "--disable-auto-launch", "--offline", "--reserve-vram", "1.0",
    ]
    return python, arguments, home


def _start() -> subprocess.Popen:
    python, arguments, home = launch_command()
    logs = Path(ROOT) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONUTF8="1")
    stdout = open(logs / "comfyui.out.log", "ab", buffering=0)
    stderr = open(logs / "comfyui.err.log", "ab", buffering=0)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [str(python), *arguments],
        cwd=str(home), env=environment,
        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        creationflags=flags, close_fds=True,
    )
    LOGGER.info("comfy_engine started pid=%s", process.pid)
    return process


def ensure_online() -> dict[str, Any]:
    """Return {'online': True, 'started': bool}. Raise if the engine cannot start."""
    if online():
        return {"online": True, "started": False}
    with LOCK:
        if online():
            return {"online": True, "started": False}
        process = _start()
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if online():
                return {"online": True, "started": True, "pid": process.pid}
            code = process.poll()
            if code is not None:
                if online():
                    return {"online": True, "started": False, "pid": process.pid}
                raise RuntimeError(
                    f"ComfyUI 启动后立即退出（code {code}），查看 director-data/logs/comfyui.err.log"
                )
            time.sleep(1)
        raise RuntimeError(f"ComfyUI 在 {START_TIMEOUT_SECONDS:.0f} 秒内未就绪")
