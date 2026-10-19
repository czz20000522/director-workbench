import shutil
import subprocess
import time
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


def test_media_cache_hits_and_invalidates_when_file_identity_changes(tmp_path, monkeypatch):
    ffmpeg = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg)
    if ffmpeg is None or ffprobe is None:
        pytest.skip('FFmpeg/ffprobe 不可用')
    left, right = tmp_path / 'left.mp4', tmp_path / 'right.mp4'
    _make_video(left, 'red')
    _make_video(right, 'blue')
    original_run = assembly_continuity.subprocess.run
    calls = []

    def counted_run(*args, **kwargs):
        calls.append(args[0][0])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(assembly_continuity.subprocess, 'run', counted_run)
    kwargs = {'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'temp_root': tmp_path}
    first = assembly_continuity.analyze_join({'video': {'path': str(left)}}, {'video': {'path': str(right)}}, **kwargs)
    assert first['status'] == 'ok'
    first_count = len(calls)
    second = assembly_continuity.analyze_join({'video': {'path': str(left)}}, {'video': {'path': str(right)}}, **kwargs)
    assert second['status'] == 'ok'
    assert len(calls) == first_count
    now = time.time_ns()
    left.touch()
    import os
    os.utime(left, ns=(now, now + 1))
    third = assembly_continuity.analyze_join({'video': {'path': str(left)}}, {'video': {'path': str(right)}}, **kwargs)
    assert third['status'] == 'ok'
    assert len(calls) > first_count


def test_join_budget_marks_excess_rows_unavailable(tmp_path):
    segments = [{'id': f'S{index:02d}', 'video': {'path': str(tmp_path / f'S{index:02d}.mp4')}} for index in range(4)]
    rows = assembly_continuity.analyze_joins(
        segments, ffmpeg=tmp_path / 'missing-ffmpeg.exe', ffprobe=tmp_path / 'missing-ffprobe.exe', max_joins=1)
    assert len(rows) == 3
    assert '接点数量超过分析上限' in rows[1]['reason']
    assert '接点数量超过分析上限' in rows[2]['reason']


def test_deadline_marks_unstarted_rows_unavailable(tmp_path):
    segments = [{'id': f'S{index:02d}', 'video': {'path': str(tmp_path / f'S{index:02d}.mp4')}} for index in range(3)]
    rows = assembly_continuity.analyze_joins(
        segments, ffmpeg=tmp_path / 'missing-ffmpeg.exe', ffprobe=tmp_path / 'missing-ffprobe.exe',
        analysis_deadline_seconds=0.000001)
    assert all(row['status'] == 'unavailable' for row in rows)
    assert all('总时间预算' in row['reason'] for row in rows)


def test_missing_audio_is_partial_and_explained(tmp_path):
    ffmpeg = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg)
    if ffmpeg is None or ffprobe is None:
        pytest.skip('FFmpeg/ffprobe 不可用')
    left, right = tmp_path / 'left.mp4', tmp_path / 'right.mp4'
    _make_video(left, 'red', audio=False)
    _make_video(right, 'blue', audio=False)
    result = assembly_continuity.analyze_join(
        {'video': {'path': str(left)}}, {'video': {'path': str(right)}}, ffmpeg=ffmpeg, ffprobe=ffprobe, temp_root=tmp_path)
    assert result['status'] == 'partial'
    assert result['available'] is False
    assert '音频轨' in result['reason']


def test_extract_failure_is_partial_and_not_available(tmp_path, monkeypatch):
    left, right = tmp_path / 'left.mp4', tmp_path / 'right.mp4'
    left.write_bytes(b'left')
    right.write_bytes(b'right')
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffprobe = tmp_path / 'ffprobe.exe'
    ffmpeg.write_bytes(b'')
    ffprobe.write_bytes(b'')
    spec = {'video': {'width': 64, 'height': 48, 'fps': 12, 'rotation': 0, 'aspect_ratio': 1.333333, 'duration_seconds': 1},
            'audio': {'sample_rate': 8000, 'channels': 1, 'present': True}}
    monkeypatch.setattr(assembly_continuity, '_probe_media', lambda *args, **kwargs: spec)
    monkeypatch.setattr(assembly_continuity, '_extract_raw_frame', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('解码失败')))
    result = assembly_continuity.analyze_join(
        {'video': {'path': str(left)}}, {'video': {'path': str(right)}}, ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert result['status'] == 'partial'
    assert result['available'] is False
    assert result['reason'] == '解码失败'
