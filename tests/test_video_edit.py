"""剪辑台的确定性行为：入点/出点、拼接、无音轨补静音、BGM 混音。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend import media_operations, video_edit


def make_clip(path: Path, seconds: float = 1.0, audio: bool = True, size: str = '320x180') -> None:
    command = [
        str(media_operations.FFMPEG), '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', f'color=c=0x335577:s={size}:r=24:d={seconds}',
    ]
    if audio:
        command += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={seconds}',
                    '-c:a', 'aac', '-ac', '2', '-shortest']
    else:
        command += ['-an']
    command += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)]
    subprocess.run(command, check=True, capture_output=True)


def test_probe_reports_duration_and_audio(tmp_path: Path) -> None:
    clip = tmp_path / 'clip.mp4'
    make_clip(clip, 1.2, audio=True)
    info = video_edit.probe(clip)
    assert info['duration'] == pytest.approx(1.2, abs=0.1)
    assert (info['width'], info['height']) == (320, 180)
    assert info['has_audio'] is True


def test_trim_concat_pads_missing_audio(tmp_path: Path) -> None:
    with_audio = tmp_path / 'a.mp4'
    without_audio = tmp_path / 'b.mp4'
    make_clip(with_audio, 1.0, audio=True)
    make_clip(without_audio, 1.0, audio=False)

    payload = video_edit.prepare('t-concat', 'p1', tmp_path, [
        {'path': str(with_audio), 'label': 'A', 'start_seconds': 0.2, 'end_seconds': 0.8},
        {'path': str(without_audio), 'label': 'B', 'start_seconds': 0.1, 'end_seconds': 0.9},
    ])
    receipt = video_edit.execute(payload)

    assert receipt['duration_seconds'] == pytest.approx(1.4, abs=0.12)
    assert (receipt['width'], receipt['height']) == (320, 180)
    assert Path(receipt['output']).is_file()
    assert video_edit.probe(Path(receipt['output']))['has_audio'] is True


def test_background_music_is_mixed(tmp_path: Path) -> None:
    clip = tmp_path / 'clip.mp4'
    make_clip(clip, 1.0, audio=False)
    tone = tmp_path / 'tone.wav'
    subprocess.run([
        str(media_operations.FFMPEG), '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000:duration=0.6', str(tone),
    ], check=True, capture_output=True)

    payload = video_edit.prepare('t-bgm', 'p1', tmp_path, [
        {'path': str(clip), 'label': 'A', 'start_seconds': 0, 'end_seconds': 1.0},
    ], background_music={'path': str(tone), 'volume': 0.4})
    receipt = video_edit.execute(payload)

    assert receipt['duration_seconds'] == pytest.approx(1.0, abs=0.12)
    assert video_edit.probe(Path(receipt['output']))['has_audio'] is True


def test_rejects_empty_window(tmp_path: Path) -> None:
    clip = tmp_path / 'clip.mp4'
    make_clip(clip, 1.0, audio=False)
    with pytest.raises(video_edit.VideoEditFailed):
        video_edit.prepare('t-bad', 'p1', tmp_path, [
            {'path': str(clip), 'label': 'A', 'start_seconds': 0.9, 'end_seconds': 0.92},
        ])
