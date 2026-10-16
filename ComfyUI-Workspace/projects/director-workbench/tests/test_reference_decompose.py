from __future__ import annotations

import sys
import types

from tools import reference_decompose


def test_recommended_frame_count_scales_with_duration_and_stays_bounded() -> None:
    assert reference_decompose.recommended_frame_count(10) == 12
    assert reference_decompose.recommended_frame_count(126.967) == 22
    assert reference_decompose.recommended_frame_count(900) == 36


def test_rel_keeps_private_storage_paths_authorized(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reference_decompose, 'ROOT', tmp_path / 'workspace-root')
    outside = tmp_path / 'private' / 'frame.jpg'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'frame')
    value = reference_decompose.rel(outside)
    assert value.replace('\\', '/') == str(outside.resolve()).replace('\\', '/')


def test_reference_audio_path_keeps_private_storage_paths_authorized(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, 'whisper', types.SimpleNamespace())
    from tools import reference_audio

    monkeypatch.setattr(reference_audio, 'ROOT', tmp_path / 'workspace-root')
    outside = tmp_path / 'private' / 'audio.wav'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'audio')
    value = reference_audio.root_relative(outside)
    assert value.replace('\\', '/') == str(outside.resolve()).replace('\\', '/')
