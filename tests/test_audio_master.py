import wave
from pathlib import Path

import numpy as np
import pytest

from backend import audio_master_tasks


def write_wav(path: Path, values: np.ndarray, rate: int = 8000, channels: int = 1) -> None:
    values = np.asarray(values, dtype=np.float64).reshape(-1, channels)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(np.rint(np.clip(values, -1, 1) * 32767).astype('<i2').tobytes())


def test_master_mix_ducking_receipt_and_pcm_output(tmp_path):
    source = tmp_path / 'master.wav'
    background = tmp_path / 'background.wav'
    write_wav(source, np.full((8000, 1), .5))
    write_wav(background, np.full((8000, 1), .25))
    payload = audio_master_tasks.prepare('master-test', source, tmp_path / 'tasks', background=background, parameters={
        'target_lufs': -18, 'true_peak_dbtp': -1, 'background_gain_db': -6,
        'ducking': {'threshold_db': -30, 'ratio': 4, 'attack_seconds': .01, 'release_seconds': .1},
    })
    receipt = audio_master_tasks.execute(payload)
    assert audio_master_tasks.read_receipt(payload) == receipt
    assert receipt['frames'] == 8000
    assert receipt['report']['parameters']['ducking']['ratio'] == 4.0
    assert receipt['report']['ffmpeg_version']
    assert receipt['report']['output_true_peak_dbtp'] <= -1 + 1e-6
    with wave.open(receipt['output'], 'rb') as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (8000, 1, 2)


def test_master_rejects_mismatched_background_and_requires_explicit_padding(tmp_path):
    source = tmp_path / 'master.wav'
    background = tmp_path / 'background.wav'
    write_wav(source, np.zeros((8000, 1)), rate=8000)
    write_wav(background, np.zeros((8000, 1)), rate=16000)
    with pytest.raises(ValueError, match='sample rate'):
        audio_master_tasks.prepare('mismatch', source, tmp_path / 'tasks', background=background, parameters={})
    short_background = tmp_path / 'short.wav'
    write_wav(short_background, np.zeros((4000, 1)), rate=8000)
    with pytest.raises(ValueError, match='shorter'):
        audio_master_tasks.execute(audio_master_tasks.prepare('short', source, tmp_path / 'tasks2', background=short_background,
                                                              parameters={'trim_duration_seconds': 1}))
    padded = audio_master_tasks.prepare('padded', source, tmp_path / 'tasks3', background=short_background,
                                        parameters={'trim_duration_seconds': 1, 'pad_silence': True})
    assert audio_master_tasks.execute(padded)['frames'] == 8000


def test_master_limits_duration_and_parameters(tmp_path):
    source = tmp_path / 'long.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.setnframes(30 * 60 * 8000 + 1)
        stream.writeframesraw(b'\0\0' * (30 * 60 * 8000 + 1))
    with pytest.raises(ValueError, match='30 minutes'):
        audio_master_tasks.prepare('long', source, tmp_path / 'tasks', parameters={})
    with pytest.raises(ValueError, match='target_lufs'):
        audio_master_tasks.validate_parameters({'target_lufs': -30})
