import json

import pytest

from tools.generate_speech import generate, load_request


def test_existing_output_is_not_overwritten(tmp_path):
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'fixture')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'task_id': 'speech-1', 'text': '你好', 'voice_reference': str(reference)}), encoding='utf-8')
    destination = tmp_path / 'existing'
    destination.mkdir()
    receipt = destination / 'receipt.json'
    receipt.write_text('original receipt', encoding='utf-8')
    with pytest.raises(FileExistsError):
        generate(request, destination)
    assert receipt.read_text(encoding='utf-8') == 'original receipt'


@pytest.mark.parametrize('changes', [{'text': ''}, {'text': '字' * 2001}, {'voice_reference': 'relative.wav'}, {'command': 'arbitrary executable'}])
def test_invalid_request_fails_before_output_creation(tmp_path, changes):
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'fixture')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'task_id': 'speech-1', 'text': '你好', 'voice_reference': str(reference), **changes}), encoding='utf-8')
    with pytest.raises(ValueError):
        generate(request, tmp_path / 'new-output')
    assert not (tmp_path / 'new-output').exists()
