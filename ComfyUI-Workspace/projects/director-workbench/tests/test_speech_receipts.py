import json
import wave
from pathlib import Path

import pytest

from backend.speech import prepare, read_receipt


def fixture(tmp_path):
    reference = tmp_path / 'voice.wav'
    reference.write_bytes(b'original reference')
    payload = prepare('speech-1', '你好', reference, tmp_path / 'snapshots')
    reference.write_bytes(b'changed reference')
    assert Path(payload['request']['voice_reference']).read_bytes() == b'original reference'
    output_dir = Path(payload['output_dir'])
    output_dir.mkdir()
    output = output_dir / 'speech.wav'
    with wave.open(str(output), 'wb') as audio:
        audio.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 22050)
    receipt = {**payload['request'], 'status': 'succeeded', 'output': str(output), 'sample_rate': 22050, 'channels': 1, 'duration_seconds': 1}
    return payload, output_dir / 'receipt.json', receipt


def test_frozen_input_and_valid_receipt(tmp_path):
    payload, path, receipt = fixture(tmp_path)
    assert payload['engine'] == 'indextts-2.5'
    path.write_text(json.dumps(receipt), encoding='utf-8')
    assert read_receipt(payload) == receipt


@pytest.mark.parametrize('change', [{'task_id': 'other'}, {'text': 'different'}, {'output': 'elsewhere.wav'}, {'duration_seconds': 2}, {'duration_seconds': 'NaN'}, {'duration_seconds': 'Infinity'}, {'sample_rate': 48000}])
def test_mismatched_receipts_are_rejected(tmp_path, change):
    payload, path, receipt = fixture(tmp_path)
    path.write_text(json.dumps({**receipt, **change}), encoding='utf-8')
    with pytest.raises(ValueError):
        read_receipt(payload)


def test_missing_receipt_does_not_imply_failure_or_retry(tmp_path):
    payload, _, _ = fixture(tmp_path)
    with pytest.raises(FileNotFoundError):
        read_receipt(payload)
