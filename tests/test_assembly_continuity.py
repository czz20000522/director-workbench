import shutil
import subprocess
from pathlib import Path

import pytest

from backend import assembly_continuity, media_operations


def _ffmpeg() -> Path | None:
    candidate = Path(media_operations.FFMPEG)
    return candidate if candidate.is_file() else (Path(shutil.which('ffmpeg')) if shutil.which('ffmpeg') else None)


def _ffprobe(ffmpeg: Path | None) -> Path | None:
    if ffmpeg:
        for name in ('ffprobe.exe', 'ffprobe'):
            candidate = ffmpeg.with_name(name)
            if candidate.is_file():
                return candidate
    return Path(shutil.which('ffprobe')) if shutil.which('ffprobe') else None


def _make_video(path: Path, color: str, *, audio: bool = True) -> None:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        pytest.skip('FFmpeg 不可用')
    command = [str(ffmpeg), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
               f'color=c={color}:s=64x48:r=12:d=0.5']
    if audio:
        command.extend(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000:duration=0.5'])
    command.extend(['-c:v', 'libx264', '-pix_fmt', 'yuv420p'])
    if audio:
        command.extend(['-c:a', 'aac', '-shortest'])
    command.extend(['-y', str(path)])
    subprocess.run(command, check=True, capture_output=True, timeout=30)


def test_analyze_two_short_videos_reports_specs_frames_and_audio(tmp_path):
    ffmpeg = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg)
    if ffmpeg is None or ffprobe is None:
        pytest.skip('FFmpeg/ffprobe 不可用')
    left = tmp_path / 'left.mp4'
    right = tmp_path / 'right.mp4'
    _make_video(left, 'red')
    _make_video(right, 'blue')
    result = assembly_continuity.analyze_join(
        {'id': 'S01', 'video': {'path': str(left)}},
        {'id': 'S02', 'video': {'path': str(right)}},
        ffmpeg=ffmpeg, ffprobe=ffprobe, temp_root=tmp_path,
    )
    assert result['advisory'] is True
    assert result['available'] is True
    assert result['video']['left']['width'] == 64
    assert result['video']['left']['height'] == 48
    assert result['frames']['available'] is True
    assert result['frames']['mean_absolute_rgb_difference'] > 0.2
    assert result['audio']['boundary']['left']['rms'] > 0
    assert 'note' in result and '非艺术判断' in result['note']


def test_analyze_document_preserves_adjacent_join_keys(tmp_path):
    result = assembly_continuity.analyze_document({'segments': [
        {'id': 'S01', 'video': {'path': str(tmp_path / 'missing-1.mp4')}},
        {'id': 'S02', 'video': {'path': str(tmp_path / 'missing-2.mp4')}},
        {'id': 'S03', 'video': {}},
    ]}, ffmpeg=tmp_path / 'missing-ffmpeg.exe', ffprobe=tmp_path / 'missing-ffprobe.exe')
    assert result['advisory'] is True
    assert [row['key'] for row in result['joins']] == ['S01>S02', 'S02>S03']
    assert all(row['status'] == 'unavailable' for row in result['joins'])
    assert all('reason' in row for row in result['joins'])


def test_analyze_join_handles_unavailable_tools_without_raising(tmp_path):
    result = assembly_continuity.analyze_join(
        {'video': {'path': str(tmp_path / 'left.mp4')}},
        {'video': {'path': str(tmp_path / 'right.mp4')}},
        ffmpeg=tmp_path / 'missing-ffmpeg.exe', ffprobe=tmp_path / 'missing-ffprobe.exe',
    )
    assert result == {
        'advisory': True, 'available': False, 'status': 'unavailable', 'warnings': [],
        'video': {}, 'frames': {}, 'audio': {},
        'note': '启发式、非艺术判断；不会自动通过或阻止装配',
        'reason': 'FFmpeg/ffprobe 不可用，未执行连续性提示',
    }
