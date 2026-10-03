"""Inspect or restore the desktop ComfyUI engine without opening a console.

Inspection is the default. Launch only during an authorized maintenance window;
this entrypoint never stops a process or submits a generation.
"""
import argparse
from contextlib import contextmanager
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
import urllib.request


ROOT = Path(__file__).resolve().parents[4]
PORT = 8188


def launch_plan(root):
    root = Path(root).resolve()
    spec = importlib.util.spec_from_file_location('engine_storage', root / 'ComfyUI-Workspace/production/storage_layout.py')
    storage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(storage)
    if not (root / 'ComfyUI-Workspace/config/storage.json').is_file():
        raise ValueError('Configure server asset roots before restoring the engine')
    roots = storage.storage_roots(root)
    install = root / 'ComfyUI-Installs/第一个comfyui配置/ComfyUI'
    shared = storage.checked_root(root / 'ComfyUI-Shared')
    python = storage.checked_root(install / '.venv/Scripts/python.exe')
    temp = storage.checked_root(shared / 'temp')
    user = storage.checked_root(install / 'user')
    for path in (install, python, install / 'main.py', shared / 'models', temp, user, roots['input'], roots['output']):
        storage.checked_root(path)
        if not path.exists():
            raise ValueError(f'Required engine path is missing: {path}')
    argv = [str(python), '-s', 'main.py', '--listen', '127.0.0.1', '--port', str(PORT),
            '--enable-manager', '--input-directory', str(roots['input']),
            '--output-directory', str(roots['output']), '--temp-directory', str(temp),
            '--user-directory', str(user)]
    model_config = install / 'extra_model_paths.yaml'
    if not model_config.is_file():
        raise ValueError('Desktop shared-model configuration is missing')
    storage.checked_root(model_config)
    argv += ['--extra-model-paths-config', str(model_config)]
    runtime = storage.checked_root(root / 'ComfyUI-Workspace/runtime/comfy-recovery')
    return dict(argv=argv, cwd=install, runtime=runtime)


def port_occupied():
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', PORT))
        except OSError:
            return True
    return False


def hidden_options():
    if os.name != 'nt':
        raise RuntimeError('Desktop engine recovery requires Windows')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return dict(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)


def process_alive(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        # Access denied is uncertainty, not proof that a previous launch ended.
        return ctypes.get_last_error() != 87
    try:
        code = ctypes.c_uint32()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def launch_lock(runtime):
    import msvcrt
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / 'launch.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def restore(plan, timeout=120):
    options = hidden_options()
    runtime = plan['runtime']
    with launch_lock(runtime):
        if port_occupied():
            raise RuntimeError('Engine port is occupied; refusing another launch')
        receipt_path = runtime / 'latest-launch.json'
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text(encoding='utf-8'))
            if previous.get('status') != 'launch_failed' and (not previous.get('pid') or process_alive(previous['pid'])):
                raise RuntimeError('Previous launch may still be active; inspect its receipt and logs')
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        receipt = dict(status='starting', stdout=str(runtime / (stamp + '.stdout.log')),
                       stderr=str(runtime / (stamp + '.stderr.log')), submitted_gpu=False)
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        environment = os.environ.copy()
        environment.pop('PYTHONHOME', None)
        environment.pop('PYTHONPATH', None)
        environment['PYTHONNOUSERSITE'] = '1'
        try:
            with open(receipt['stdout'], 'wb') as out, open(receipt['stderr'], 'wb') as err:
                process = subprocess.Popen(plan['argv'], cwd=plan['cwd'], env=environment,
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
        except OSError:
            receipt.update(status='launch_failed')
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
            raise
        receipt.update(pid=process.pid, status='health_pending')
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                receipt.update(status='exited', exit_code=process.returncode)
                receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
                raise RuntimeError('Engine exited; inspect the recorded logs')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/system_stats', timeout=2) as response:
                    stats = json.load(response)
                if not isinstance(stats.get('system'), dict):
                    raise ValueError('Invalid engine health response')
                receipt.update(status='healthy', comfyui_version=stats['system'].get('comfyui_version'))
                receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
                return receipt
            except (OSError, ValueError):
                time.sleep(0.5)
        raise RuntimeError('Engine health pending; inspect the existing launch, do not launch again')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--launch', action='store_true', help='Start one engine only if the port is free')
    options = parser.parse_args(argv)
    plan = launch_plan(options.root)
    result = restore(plan) if options.launch else {**plan, 'port_occupied': port_occupied(), 'submitted_gpu': False}
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
