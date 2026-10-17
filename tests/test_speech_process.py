import subprocess
import sys

import pytest

from backend import speech


@pytest.mark.parametrize('mode', ['timeout', 'cancel', 'callback_failure'])
def test_running_owned_process_is_reaped(tmp_path, monkeypatch, mode):
    actual_popen = subprocess.Popen
    owned = []
    def launch(command, **kwargs):
        process = actual_popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=kwargs['stdout'], stderr=kwargs['stderr'])
        owned.append(process)
        return process
    monkeypatch.setattr(speech.subprocess, 'Popen', launch)
    # taskkill uses subprocess.run, which also uses Popen: restore that call's implementation.
    actual_stop = speech.stop_owned_process
    def stop(process):
        monkeypatch.setattr(speech.subprocess, 'Popen', actual_popen)
        actual_stop(process)
    monkeypatch.setattr(speech, 'stop_owned_process', stop)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        if calls > 1 and mode == 'callback_failure':
            raise ValueError('task store unavailable')
        return calls > 1 and mode == 'cancel'
    error = {'timeout': speech.SpeechFailed, 'cancel': speech.SpeechStopped, 'callback_failure': ValueError}[mode]
    with pytest.raises(error):
        speech.execute({'request_path': str(tmp_path / 'request.json'), 'output_dir': str(tmp_path / 'result')}, cancelled, timeout=0.1)
    assert len(owned) == 1
    assert owned[0].poll() is not None


def test_cancelled_before_start_never_launches(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('cancelled task launched a process')
    monkeypatch.setattr(speech.subprocess, 'Popen', unexpected)
    with pytest.raises(speech.SpeechStopped):
        speech.execute({}, lambda: True)


def test_unknown_engine_does_not_silently_run_default_model(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('unsupported engine launched a process')
    monkeypatch.setattr(speech.subprocess, 'Popen', unexpected)
    with pytest.raises(speech.SpeechFailed, match='不会改用其他模型'):
        speech.execute({'engine': 'not-installed'}, lambda: False)


@pytest.mark.parametrize('detail,expected', [
    ('ValueError: Reference plus requested generation must not exceed 30 seconds', '合计超过 30 秒'),
    ('torch.OutOfMemoryError: CUDA out of memory', '显存不足'),
    ('soundfile.LibsndfileError: Error opening reference', '音色参考无法解码'),
    ('RuntimeError: unexpected failure', '退出码 1'),
])
def test_failed_worker_reports_actionable_reason(tmp_path, monkeypatch, detail, expected):
    class Failed:
        returncode = 1
        def poll(self):
            return 1
    def launch(command, **kwargs):
        kwargs['stdout'].write(('loading model\n' + detail).encode('utf-8'))
        return Failed()
    monkeypatch.setattr(speech.subprocess, 'Popen', launch)
    with pytest.raises(speech.SpeechFailed, match=expected):
        speech.execute({'request_path': str(tmp_path / 'request.json'),
                        'output_dir': str(tmp_path / 'result')}, lambda: False)
