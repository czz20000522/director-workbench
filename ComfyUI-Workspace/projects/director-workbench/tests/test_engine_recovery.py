import os
import subprocess
import sys

import pytest

from tools import restore_comfy_engine as recovery


@pytest.mark.skipif(os.name != 'nt', reason='Windows console behavior')
def test_hidden_launch_runs_child_without_visible_console(tmp_path):
    # The actual child inspects its own Win32 console visibility.
    code = ('import ctypes; u=ctypes.windll.user32; k=ctypes.windll.kernel32; '
            'k.GetConsoleWindow.restype=ctypes.c_void_p; '
            'u.IsWindowVisible.argtypes=[ctypes.c_void_p]; '
            'h=k.GetConsoleWindow(); assert not h or not u.IsWindowVisible(h); print("hidden")')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            timeout=10, check=True, **recovery.hidden_options())
    assert result.stdout.strip() == 'hidden'


@pytest.mark.skipif(os.name != 'nt', reason='Windows engine recovery')
def test_busy_port_refuses_before_any_process_start(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'port_occupied', lambda: True)
    def forbidden(*args, **kwargs):
        pytest.fail('An occupied port must not start another engine')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='port is occupied'):
        recovery.restore({'runtime': tmp_path})
    assert not (tmp_path / 'latest-launch.json').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows process handle behavior')
def test_prior_live_launch_blocks_even_before_listener_ready(tmp_path, monkeypatch):
    import json
    (tmp_path / 'latest-launch.json').write_text(json.dumps({'pid': os.getpid()}))
    monkeypatch.setattr(recovery, 'port_occupied', lambda: False)
    def forbidden(*args, **kwargs):
        pytest.fail('A warming-up engine must not start again')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='Previous launch'):
        recovery.restore({'runtime': tmp_path})
