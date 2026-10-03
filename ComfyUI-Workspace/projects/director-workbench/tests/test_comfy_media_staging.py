import io
import json
from urllib.error import HTTPError

import pytest

from backend import app as backend


def test_relative_inputs_are_frozen_without_touching_source(tmp_path, monkeypatch):
    inputs = tmp_path / 'input'
    inputs.mkdir()
    source = inputs / 'voice.wav'
    source.write_bytes(b'original voice')
    monkeypatch.setattr(backend, 'INPUT_ROOT', inputs)
    graph = {'audio': {'class_type': 'LoadAudio', 'inputs': {'audio': 'voice.wav [input]'}}}
    records = backend.stage_comfy_input_files(graph, 'test-project')
    relative = graph['audio']['inputs']['audio']
    assert relative.startswith('导演工作台工作目录/test-project/任务素材/')
    assert records[0]['source'] == str(source)
    source.write_bytes(b'new voice')
    assert (inputs / relative).read_bytes() == b'original voice'


def test_staging_rejects_input_path_escape(tmp_path, monkeypatch):
    inputs = tmp_path / 'input'
    inputs.mkdir()
    (tmp_path / 'outside.png').write_bytes(b'outside')
    monkeypatch.setattr(backend, 'INPUT_ROOT', inputs)
    with pytest.raises(ValueError, match='越界'):
        backend.stage_comfy_input_files({'image': {'class_type': 'LoadImage', 'inputs': {'image': '../outside.png'}}}, 'test')
    assert not list(inputs.rglob('*'))


def test_comfy_validation_error_includes_actionable_node_details():
    body = {'node_errors': {'114': {'class_type': 'LoadImage', 'errors': [{'message': 'Custom validation failed', 'details': 'Invalid image file: missing.png'}]}}}
    exc = HTTPError('http://localhost/prompt', 400, 'Bad Request', {}, io.BytesIO(json.dumps(body).encode()))
    message = backend.comfy_submission_error(exc)
    assert 'LoadImage' in message and 'missing.png' in message
    assert '输入校验失败' in message
