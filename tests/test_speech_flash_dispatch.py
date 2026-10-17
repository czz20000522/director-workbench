import json
from pathlib import Path

import pytest

from backend import speech


def test_flash_request_freezes_engine_and_parameters(tmp_path):
    voice = tmp_path / 'voice.wav'
    voice.write_bytes(b'frozen reference')
    payload = speech.prepare('flash-1', '你好', voice, tmp_path / 'tasks',
                             engine='auk-flash', gen_seconds=6, seed=42)
    request = json.loads(Path(payload['request_path']).read_text(encoding='utf-8'))
    assert request == payload['request']
    assert request['engine'] == payload['engine'] == 'auk-flash'
    assert request['parameters'] == {'gen_seconds': 6, 'seed': 42, 'nfe': 4,
                                     'cfg_strength': 0.0, 'dtype': 'bf16', 'cpu_offload': True}
    voice.write_bytes(b'changed')
    assert Path(request['voice_reference']).read_bytes() == b'frozen reference'


@pytest.mark.parametrize('parameters', [{'seed': True}, {'seed': -1}, {'seed': 2**32},
    {'gen_seconds': float('nan')}, {'gen_seconds': float('inf')}, {'gen_seconds': 0},
    {'gen_seconds': 31}, {'gen_seconds': True}])
def test_invalid_flash_parameters_leave_no_snapshot(tmp_path, parameters):
    with pytest.raises(ValueError):
        speech.prepare('flash-1', '你好', tmp_path / 'voice.wav', tmp_path / 'tasks',
                       engine='auk-flash', **parameters)
    assert not (tmp_path / 'tasks').exists()


@pytest.mark.parametrize('engine', [None, 'indextts-2.5', 'auk-flash'])
def test_frozen_engine_selects_runtime(tmp_path, monkeypatch, engine):
    calls = []
    class Finished:
        returncode = 0
        def poll(self):
            return 0
    def launch(command, **kwargs):
        calls.append((command, kwargs['cwd']))
        return Finished()
    monkeypatch.setattr(speech.subprocess, 'Popen', launch)
    monkeypatch.setattr(speech, 'read_receipt', lambda _: {'status': 'succeeded'})
    payload = {'request_path': str(tmp_path / 'request.json'), 'output_dir': str(tmp_path / 'result')}
    if engine:
        payload['engine'] = engine
    if engine == 'auk-flash':
        payload['request'] = {'engine': engine}
    assert speech.execute(payload, lambda: False)['status'] == 'succeeded'
    runtime = speech.FLASH_ROOT if engine == 'auk-flash' else speech.TTS_ROOT
    adapter = speech.FLASH_ADAPTER if engine == 'auk-flash' else speech.ADAPTER
    assert calls == [([str(runtime / '.venv/Scripts/python.exe'), '-u', str(adapter),
                      '--request', payload['request_path'], '--output-dir', payload['output_dir']], runtime)]


@pytest.mark.parametrize('engine,frozen_request', [('auk-flash', {}), ('indextts-2.5', {'engine': 'auk-flash'})])
def test_mismatched_engine_never_launches(monkeypatch, engine, frozen_request):
    monkeypatch.setattr(speech.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(speech.SpeechFailed, match='引擎与冻结请求不一致'):
        speech.execute({'engine': engine, 'request': frozen_request}, lambda: False)
