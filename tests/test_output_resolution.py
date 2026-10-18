from pathlib import Path
import json
import subprocess
import pytest
import av

from backend import output_resolution as output
from backend import app as backend
from test_keyframe_inputs import reference_image
from test_production_presets import preset_client

# The real resize/re-encode cases need the installed FFmpeg.
pytestmark = pytest.mark.host_tool


@pytest.mark.parametrize('resolution,aspect,size', [
    ('2k', '16:9', (2048, 1152)), ('4k', '9:16', (2160, 3840)), ('8k', '1:1', (7680, 7680)),
])
def test_real_image_size_and_reconciliation(tmp_path, resolution, aspect, size):
    source = reference_image(tmp_path)
    original = source.read_bytes()
    result, receipt = output.resize(source, tmp_path, resolution, aspect)
    assert result != source and source.read_bytes() == original
    with av.open(str(result)) as media:
        frame = next(media.decode(video=0))
        assert (frame.width, frame.height) == size
    timestamp = result.stat().st_mtime_ns
    assert output.resize(source, tmp_path, resolution, aspect) == (result, receipt)
    assert result.stat().st_mtime_ns == timestamp


@pytest.mark.parametrize('resolution,size', [('2k', (2048, 1152)), ('4k', (3840, 2160)), ('8k', (7680, 4320))])
def test_real_streaming_video_preserves_frames_and_audio(tmp_path, resolution, size):
    source = tmp_path / 'tiny.mp4'
    subprocess.run([str(output.FFMPEG), '-nostdin', '-n', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=64x64:r=24:d=0.25', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
        '-t', '0.25', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True)
    target, receipt = output.resize(source, tmp_path, resolution, '16:9')
    assert receipt['method'] == 'lanczos_resize'
    with av.open(str(target)) as media:
        assert len(media.streams.audio) == 1
        assert (media.streams.video[0].width, media.streams.video[0].height) == size
        assert len(list(media.decode(video=0))) == 6


def test_bounds_native_and_stop(tmp_path):
    source = reference_image(tmp_path)
    assert output.resize(source, tmp_path, 'native', '1:1') == (source, None)
    with pytest.raises(ValueError):
        output.resize(source, tmp_path / 'other', '4k', '16:9')
    with pytest.raises(ValueError):
        output.dimensions('100k', '16:9')
    with pytest.raises(output.OutputStopped):
        output.resize(source, tmp_path, '8k', '1:1', lambda: True)
    assert not list(tmp_path.glob('*-output-*'))


def test_image_request_freezes_size_and_rejects_changed_retry(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    args = {'mode': 'text', 'prompt': 'Coastal sunrise', 'idempotency_key': 'resolution', 'output_resolution': '4k'}
    endpoint = '/api/projects/preset-test/keyframe-tasks'
    response = client.post(endpoint, json=args)
    assert response.status_code == 202, response.text
    task = response.json()
    assert task['payload']['output_size'] == {'resolution': '4k', 'aspect_ratio': '9:16'}
    assert client.post(endpoint, json=args).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'output_resolution': '8k'}).status_code == 409
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['6']['inputs']['width'] == 768, 'Native model size must stay bounded'
    assert client.post(endpoint, json={**args, 'output_resolution': '100k'}).status_code == 422
    monkeypatch.setattr(output, 'FFMPEG', project / 'missing-tool.exe')
    assert client.post(endpoint, json=args).json()['id'] == task['id'], 'Receipt recovery must not need generation resources'
    assert client.post(endpoint, json={**args, 'idempotency_key': 'another'}).status_code == 422


def test_public_version_snapshot_exposes_processing_profile():
    snapshot = backend.public_version_snapshot({'prompt': 'A lighthouse'}, {
        'output_size': {'resolution': '4k', 'width': 3840, 'height': 2160, 'method': 'lanczos_resize'},
    })
    assert snapshot['output_size']['resolution'] == '4k'
    assert snapshot['processing_method'] == 'lanczos_resize'
