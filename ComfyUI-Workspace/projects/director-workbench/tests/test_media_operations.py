import json
from pathlib import Path
import subprocess
import wave

import pytest

from backend import media_operations as media


def test_silence_real_pcm_and_receipt(tmp_path):
    payload = media.prepare('silence-1', 'silence', None, tmp_path, {'duration_seconds': .1, 'channels': 2})
    receipt = media.execute(payload)
    audio = Path(receipt['outputs'][0]['path'])
    with wave.open(str(audio)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()) == (2, 2, 24000, 2400)
        assert not any(stream.readframes(2400))
    assert media.read_receipt(payload) == receipt
    assert media.owner_alive(payload) is False


@pytest.mark.parametrize('operation,parameters', [('silence', {'duration_seconds': 121}), ('silence', {'duration_seconds': 1, 'sample_rate': True}), ('video_qc', {'sample_count': 61}), ('transcribe', {'language': 'xx'}), ('silent_video', {'path': 'x'})])
def test_bad_parameters(operation, parameters):
    with pytest.raises(ValueError):
        media.validate_parameters(operation, parameters)


def test_cancel_before_execution_and_missing_owner(tmp_path):
    payload = media.prepare('cancel', 'silence', None, tmp_path, {'duration_seconds': 1})
    with pytest.raises(media.MediaOperationStopped):
        media.execute(payload, lambda: True)
    with pytest.raises(FileNotFoundError):
        media.read_receipt(payload)
    with pytest.raises(media.MediaOperationUncertain):
        media.owner_alive(payload)


def test_existing_worker_requires_reconciliation(tmp_path):
    payload = media.prepare('unknown-worker', 'silence', None, tmp_path, {'duration_seconds': 1})
    (Path(payload['request_path']).parent / 'worker.json').write_text('{}', encoding='utf-8')
    with pytest.raises(media.MediaOperationUncertain):
        media.execute(payload)
    assert not issubclass(media.MediaOperationUncertain, media.MediaOperationFailed)


def test_failed_termination_is_uncertain(monkeypatch):
    class Process:
        pid = 123456789
        def poll(self):
            return None
        def kill(self):
            raise OSError('Cannot kill')
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(media.MediaOperationUncertain):
        media._stop(Process())


def test_receipt_publication_is_atomic(tmp_path, monkeypatch):
    from tools.process_media import publish_receipt
    real_replace = Path.replace
    observed = []
    receipt = {'status': 'succeeded', 'text': '铜钱'}
    def inspect_replace(source, target):
        assert source.parent == target.parent
        assert not target.exists()
        assert json.loads(source.read_text(encoding='utf-8')) == receipt
        observed.append(True)
        return real_replace(source, target)
    monkeypatch.setattr(Path, 'replace', inspect_replace)
    publish_receipt(tmp_path, receipt)
    assert observed and json.loads((tmp_path / 'receipt.json').read_text(encoding='utf-8')) == receipt


def test_cancel_running_worker_stops_process(tmp_path, monkeypatch):
    import time

    payload = media.prepare('running-cancel', 'silence', None, tmp_path, {'duration_seconds': 1})
    # Use the desktop environment's existing base executable: its venv launcher
    # exits when its child is killed, racing taskkill's final parent termination.
    base_python = subprocess.run([str(media.DESKTOP_PYTHON), '-c', 'import sys; print(sys._base_executable)'],
                                 capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    assert Path(base_python).is_file()
    ready_path = tmp_path / 'ready.json'
    child_script = 'import os, time; print(os.getpid(), flush=True); time.sleep(60)'
    parent_script = '''
import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', sys.argv[2]], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
assert int(child.stdout.readline()) == child.pid
assert child.poll() is None
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'parent_pid': os.getpid(), 'child_pid': child.pid}), encoding='utf-8')
temporary.replace(ready)
time.sleep(60)
'''
    real_popen = subprocess.Popen
    children = []
    def slow_worker(args, **kwargs):
        child = real_popen([base_python, '-c', parent_script, str(ready_path), child_script], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(media.subprocess, 'Popen', slow_worker)
    # taskkill also uses Popen internally, so restore it after launching the worker.
    deadline = time.monotonic() + 15
    identities = {}
    def cancellation():
        if children:
            monkeypatch.setattr(media.subprocess, 'Popen', real_popen)
            if ready_path.is_file():
                ready = json.loads(ready_path.read_text(encoding='utf-8'))
                assert ready['parent_pid'] == children[0].pid
                for label, pid in ready.items():
                    identities[label] = (pid, media._process_identity(pid))
                    assert identities[label][1] is not None, f'{label} exited before cancellation'
                return True
            assert time.monotonic() < deadline, 'The owned worker tree never reported readiness'
        return False
    with pytest.raises(media.MediaOperationStopped):
        media.execute(payload, cancellation)
    assert children[0].poll() is not None
    assert set(identities) == {'parent_pid', 'child_pid'}
    for pid, _ in identities.values():
        assert media._process_identity(pid) is None, f'Owned worker {pid} is still alive'
    assert media.owner_alive(payload) is False
    with pytest.raises(FileNotFoundError):
        media.read_receipt(payload)


def test_taskkill_255_is_uncertain_even_after_parent_exits(monkeypatch):
    from types import SimpleNamespace

    class Process:
        pid = 123456789
        exited = False
        def poll(self):
            return 1 if self.exited else None
        def wait(self, **kwargs):
            pytest.fail('Parent exit cannot confirm termination of the whole process tree')
    process = Process()
    def failed_taskkill(args, **kwargs):
        assert args == ['taskkill', '/PID', str(process.pid), '/T', '/F']
        process.exited = True
        return subprocess.CompletedProcess(args, 255, b'Child terminated', b'There is no running instance of the task')
    monkeypatch.setattr(media, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(media.subprocess, 'run', failed_taskkill)
    with pytest.raises(media.MediaOperationUncertain):
        media._stop(process)
    assert process.poll() == 1


def test_receipt_and_request_tampering(tmp_path):
    payload = media.prepare('tamper', 'silence', None, tmp_path, {'duration_seconds': .01})
    media.execute(payload)
    path = Path(payload['output_dir']) / 'receipt.json'
    receipt = json.loads(path.read_text(encoding='utf-8'))
    receipt['outputs'][0]['path'] = str(tmp_path / 'outside.wav')
    path.write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(media.MediaOperationFailed):
        media.read_receipt(payload)
    Path(payload['request_path']).write_text('{}', encoding='utf-8')
    with pytest.raises(media.MediaOperationFailed):
        media.read_receipt(payload)


def test_source_snapshot_tampering(tmp_path):
    source = tmp_path / 'audio.wav'
    source.write_bytes(b'hello')
    payload = media.prepare('source-check', 'transcribe', source, tmp_path / 'jobs', {})
    Path(payload['request']['source']['path']).write_bytes(b'changed')
    with pytest.raises(media.MediaOperationFailed):
        media.read_receipt(payload)


def test_real_video_qc_and_lossless_silent_export(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run([str(media.FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=64x48:rate=12:duration=0.5', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True, timeout=30)
    original = source.read_bytes()
    qc = media.execute(media.prepare('qc', 'video_qc', source, tmp_path / 'jobs', {'sample_count': 3}))
    assert qc['report']['frame_count'] == 6
    assert qc['report']['sampled_frame_indices'] == [0, 2, 5]
    assert qc['report']['audio_peak'] > 0
    assert len([x for x in qc['outputs'] if x['kind'] == 'image']) == 4
    silent = media.execute(media.prepare('silent', 'silent_video', source, tmp_path / 'jobs', {}))
    assert silent['report']['after']['audio_streams'] == 0
    assert silent['report']['before']['frame_count'] == silent['report']['after']['frame_count']
    destination = silent['outputs'][0]['path']
    def decoded(path):
        return subprocess.run([str(media.FFMPEG), '-v', 'error', '-i', str(path), '-map', '0:v:0', '-f', 'framemd5', '-'], capture_output=True, check=True).stdout
    assert decoded(source) == decoded(destination)
    assert source.read_bytes() == original


def test_transcription_mock_cpu_and_no_expected_prompt(tmp_path):
    # Run in the existing ASR environment, which owns numpy/soundfile; never load/download a model.
    script = '''
import sys, wave
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.process_media import transcribe
root=Path(sys.argv[2]); source=root/'source.wav'
with wave.open(str(source),'wb') as w:
 w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(3200))
class Model:
 def transcribe(self,samples,**kwargs):
  assert 'initial_prompt' not in kwargs
  assert kwargs['fp16'] is False
  return {'text':'实际识别','language':'zh','segments':[{'words':[{'word':'实际','start':0,'end':0.1}]}]}
def loader(path,**kwargs):
 assert kwargs=={'device':'cpu'}
 assert Path(path).is_file()
 return Model()
r,a=transcribe(source,root,{'language':'zh','word_timestamps':True,'expected_text':'预期文本'},loader)
assert r['text']=='实际识别' and not r['approval'] and r['text_similarity']==0
assert len(r['words'])==1
'''
    subprocess.run([str(media.ASR_PYTHON), '-c', script, str(media.ROOT), str(tmp_path)], check=True, timeout=60)
