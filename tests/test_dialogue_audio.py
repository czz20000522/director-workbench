"""台词工位：把上一条视频的音轨截成音色参考。

口型这条链是「录一句参考 → 克隆音色读新台词 → 喂给视频」，
第一步就是这里：只截一小段，别把整条片子丢给配音引擎。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend import media_operations


@pytest.fixture
def client_with_project(preset_client):
    client, project = preset_client
    return client, project


def _make_clip(target: Path, seconds: float = 1.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', f'testsrc=size=160x120:rate=12:duration={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-shortest',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(target)],
                   check=True, timeout=120)
    return target


@pytest.mark.skipif(not Path(media_operations.FFMPEG).is_file(), reason='本机没有 ffmpeg')
def test_extract_dialogue_audio_returns_a_short_wav(client_with_project, tmp_path):
    client, project = client_with_project
    clip = _make_clip(project / 'workspaces' / 'preset-test' / 'clip.mp4', 1.5)

    response = client.post('/api/projects/preset-test/dialogue/audio',
                           json={'source': str(clip), 'seconds': 1.0})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['path'].endswith('.wav')
    assert 0.5 <= body['duration_seconds'] <= 1.2
    output = Path(body['path'])
    if not output.is_file():
        output = tmp_path / 'data-root' / body['path']
    assert output.is_file() and output.stat().st_size > 2048


@pytest.mark.skipif(not Path(media_operations.FFMPEG).is_file(), reason='本机没有 ffmpeg')
def test_extract_dialogue_audio_rejects_a_video_without_sound(client_with_project):
    client, project = client_with_project
    clip = project / 'workspaces' / 'preset-test' / 'silent.mp4'
    clip.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(media_operations.FFMPEG), '-nostdin', '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=12:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)], check=True, timeout=120)

    response = client.post('/api/projects/preset-test/dialogue/audio',
                           json={'source': str(clip), 'seconds': 1.0})
    assert response.status_code == 422
    assert '音轨' in response.text


def test_extract_dialogue_audio_refuses_paths_outside_the_sandbox(client_with_project):
    client, _ = client_with_project
    response = client.post('/api/projects/preset-test/dialogue/audio',
                           json={'source': 'C:/Windows/System32/not-here.mp4', 'seconds': 1.0})
    assert response.status_code in (403, 404)
