"""Explicit 2K/4K delivery upscale candidates (Issue #118 boundary).

These tests pin only the observable contract:

* target/method/audio are explicit request fields;
* the source must be an explicitly adopted candidate/final;
* 4K is a candidate size ceiling, not native generation;
* missing tools are structured blockers, never a silent method change;
* the generated candidate is a new file and never replaces or adopts the source.

The one real encode is marked ``host_tool``; the remaining contract tests use
stubs so the fast layer still covers them without FFmpeg.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from backend import app as backend
from backend import delivery_upscale
from backend.delivery_upscale import UpscaleError, target_dimensions
from backend.media_operations import FFMPEG
from backend.routers.delivery_upscale import ROUTER


CAPABILITY_PATH = '/api/projects/{project_id}/upscale-capability'
TASKS_PATH = '/api/projects/{project_id}/upscale-tasks'


@pytest.fixture
def upscale_client(preset_client):
    client, project = preset_client
    paths = {getattr(route, 'path', '') for route in backend.app.routes}
    if CAPABILITY_PATH not in paths:
        backend.app.include_router(ROUTER)
    return client, project


def _write_plan(project: Path, source: Path, *, segment_id: str = 'S01') -> None:
    workspace = project / 'workspaces' / 'preset-test'
    assets = workspace / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    document, plan_path = backend.load_project_plan('preset-test')
    document.update({
        'revision': 1,
        'segments': [{
            'id': segment_id, 'start_seconds': 0, 'duration_seconds': 1,
            'status': 'video_generated', 'current_version_task_id': 'task-v1',
            'comfyui_task_id': 'prompt-1',
            'video': {'path': str(source), 'variant': 'v3-A'},
        }],
        'assembly': None,
    })
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')


def _adopt_sample(client, source: Path, *, segment_id: str = 'S01') -> None:
    response = client.post('/api/projects/preset-test/reviews', json={
        'asset_id': segment_id, 'stage': 'sample', 'status': 'approved',
        'note': '测试用明确采用记录', 'candidate_ref': str(source),
        'expected_revision': 0, 'expected_plan_revision': 1,
        'source': 'explicit-adoption', 'adopted_variant': 'A',
    })
    assert response.status_code == 200, response.text


def _make_video(path: Path, *, width: int = 1344, height: int = 768, seconds: float = 1.0,
                audio: bool = True) -> None:
    if not FFMPEG.is_file():
        pytest.skip('local FFmpeg is required for the host-tool fixture')
    command = [str(FFMPEG), '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'lavfi', '-i', f'testsrc=size={width}x{height}:rate=12']
    if audio:
        command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    command += ['-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                '-pix_fmt', 'yuv420p']
    command += ['-c:a', 'aac', '-shortest'] if audio else ['-an']
    command += [str(path)]
    # Write the tool log to a file instead of pipes: the isolated preset fixture
    # replaces threading.Thread, and captured pipes need reader threads.
    log = path.with_suffix('.ffmpeg.log')
    with log.open('wb') as handle:
        result = subprocess.run(command, stdout=handle, stderr=handle, timeout=120)
    assert result.returncode == 0, log.read_text(encoding='utf-8', errors='replace')[-800:]
    assert path.is_file() and path.stat().st_size > 0


def _fake_available_capability() -> dict:
    return {
        'schema_version': 1,
        'target_resolutions': {'2k': {'long_edge': 2048}, '4k': {'long_edge': 3840}},
        'methods': {
            'lanczos_only': {'available': True, 'native_detail': False, 'requires': ['ffmpeg'],
                             'blockers': [], 'note': 'stub'},
            'superres_2x+lanczos': {'available': False, 'native_detail': True, 'requires': ['ffmpeg'],
                                    'blockers': [{'code': 'superres_tool_missing', 'message': 'stub'}], 'note': 'stub'},
        },
        'methods_order': ['superres_2x+lanczos', 'lanczos_only'],
        'source_slots': ['sample', 'finish', 'final'],
        'input_limits': {'max_source_pixels': 8294400, 'max_source_long_edge': 3840,
                         'target_must_exceed_source': True, 'note': 'stub'},
        'preserve_audio': {'required': True, 'note': 'stub'},
        'execution': 'stub',
    }


def test_capability_is_factual_and_marks_unavailable_methods():
    facts = delivery_upscale.capability()
    assert facts['methods_order'] == ['superres_2x+lanczos', 'lanczos_only']
    assert facts['target_resolutions']['2k']['long_edge'] == 2048
    assert facts['target_resolutions']['4k']['long_edge'] == 3840
    assert facts['source_slots'] == ['sample', 'finish', 'final']
    assert facts['methods']['lanczos_only']['native_detail'] is False
    assert facts['methods']['superres_2x+lanczos']['native_detail'] is True
    assert facts['input_limits']['target_must_exceed_source'] is True
    assert '不是原生 4K' in facts['input_limits']['note']
    assert 'recommended' not in json.dumps(facts, ensure_ascii=False)


def test_capability_detects_missing_ffmpeg_and_superres_runner(monkeypatch):
    monkeypatch.setattr(delivery_upscale, 'FFMPEG', Path('missing-ffmpeg.exe'))
    monkeypatch.delenv('DIRECTOR_SUPERRES_COMMAND', raising=False)
    facts = delivery_upscale.capability()
    assert facts['methods']['lanczos_only']['available'] is False
    assert [item['code'] for item in facts['methods']['lanczos_only']['blockers']] == ['ffmpeg_missing']
    superres = facts['methods']['superres_2x+lanczos']
    assert superres['available'] is False
    assert 'superres_tool_missing' in {item['code'] for item in superres['blockers']}


def test_capability_detects_configured_superres_runner(monkeypatch, tmp_path):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'stub')
    model = tmp_path / 'RealESRGAN_x2plus.pth'
    model.write_bytes(b'stub')
    monkeypatch.setattr(delivery_upscale, 'FFMPEG', ffmpeg)
    monkeypatch.setenv('DIRECTOR_SUPERRES_COMMAND', 'runner.exe -i {input} -o {output}')
    monkeypatch.setattr(delivery_upscale, '_model_path', lambda: model)
    facts = delivery_upscale.capability()
    assert facts['methods']['superres_2x+lanczos']['available'] is True
    assert facts['methods']['superres_2x+lanczos']['runner_configured'] is True
    assert facts['methods']['superres_2x+lanczos']['blockers'] == []


def test_target_dimensions_preserve_aspect_and_enforce_4k_input_limits():
    two_k = target_dimensions(1344, 768, '2k')
    assert two_k['width'] == 2048 and two_k['height'] == 1170
    four_k = target_dimensions(1344, 768, '4k')
    assert four_k['width'] == 3840 and four_k['height'] == 2194
    assert target_dimensions(1080, 1920, '2k') == {
        'resolution': '2k', 'width': 1152, 'height': 2048, 'long_edge': 2048,
        'aspect_preserved': True, 'source_aspect': '9:16'}
    with pytest.raises(UpscaleError) as at_target:
        target_dimensions(3840, 2160, '4k')
    assert at_target.value.code == 'source_not_below_target'
    with pytest.raises(UpscaleError) as too_large:
        target_dimensions(5120, 2880, '4k')
    assert too_large.value.code == 'source_exceeds_4k_input_limit'


def test_capability_endpoint_returns_project_scoped_facts(upscale_client):
    client, _ = upscale_client
    response = client.get('/api/projects/preset-test/upscale-capability')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['project_id'] == 'preset-test'
    assert set(body['methods']) == {'superres_2x+lanczos', 'lanczos_only'}


def test_request_requires_explicit_enums_and_returns_structured_blocker(upscale_client, monkeypatch):
    client, _ = upscale_client
    stub = Path('stub-ffmpeg.exe')
    monkeypatch.setattr(delivery_upscale, 'capability', lambda: _fake_available_capability())
    base = {'source_asset_id': 'S01', 'source_slot': 'sample', 'target_resolution': '2k',
            'method': 'lanczos_only', 'preserve_audio': True, 'idempotency_key': 'k1'}
    for change in ({'target_resolution': '8k'}, {'method': 'magic'},
                   {'source_slot': 'auto'}, {'preserve_audio': None}):
        response = client.post('/api/projects/preset-test/upscale-tasks', json={**base, **change})
        assert response.status_code == 422, (change, response.text)
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_submission_without_explicit_adoption_is_blocked(upscale_client, monkeypatch):
    client, project = upscale_client
    source = project / 'workspaces/preset-test/assets/source.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'not-decoded-by-this-test')
    _write_plan(project, source)
    monkeypatch.setattr(delivery_upscale, 'capability', lambda: _fake_available_capability())
    response = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'target_resolution': '2k',
        'method': 'lanczos_only', 'preserve_audio': True, 'idempotency_key': 'not-adopted'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'source_not_adopted'
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_4k_source_is_rejected_before_a_task_is_created(upscale_client, monkeypatch):
    client, project = upscale_client
    source = project / 'workspaces/preset-test/assets/source.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'fixture')
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(delivery_upscale, 'capability', lambda: _fake_available_capability())
    monkeypatch.setattr(backend, 'authorized_media_facts', lambda path: {
        'known': True, 'width': 3840, 'height': 2160, 'duration_seconds': 1.0,
        'fps': 12.0, 'frames': 12, 'audio_streams': 1})
    response = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'target_resolution': '4k',
        'method': 'lanczos_only', 'preserve_audio': True, 'idempotency_key': 'four-k-input'})
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'source_not_below_target'
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_missing_ffmpeg_returns_structured_blocker_without_task(upscale_client, monkeypatch):
    client, _ = upscale_client
    monkeypatch.setattr(delivery_upscale, 'FFMPEG', Path('missing-ffmpeg.exe'))
    response = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'target_resolution': '2k',
        'method': 'lanczos_only', 'preserve_audio': True, 'idempotency_key': 'no-ffmpeg'})
    assert response.status_code == 503, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'upscale_tool_unavailable'
    assert 'ffmpeg_missing' in {item['code'] for item in detail['blockers']}
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_missing_superres_runner_is_not_silently_downgraded(upscale_client, monkeypatch):
    client, _ = upscale_client
    monkeypatch.delenv('DIRECTOR_SUPERRES_COMMAND', raising=False)
    response = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample', 'target_resolution': '2k',
        'method': 'superres_2x+lanczos', 'preserve_audio': True, 'idempotency_key': 'no-superres'})
    assert response.status_code == 503, response.text
    detail = response.json()['detail']
    assert 'superres_tool_missing' in {item['code'] for item in detail['blockers']}
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


@pytest.mark.host_tool
@pytest.mark.parametrize(('target_resolution', 'preserve_audio'), [
    ('2k', True), ('4k', True), ('2k', False)])
def test_lanczos_only_generates_a_new_candidate_without_overwriting_source(
        upscale_client, target_resolution, preserve_audio):
    client, project = upscale_client
    source = project / 'workspaces/preset-test/assets/source.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    _make_video(source)
    _write_plan(project, source)
    _adopt_sample(client, source)
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_stat = source.stat()
    plan_before = client.get('/api/projects/preset-test/plan').json()
    expected = target_dimensions(1344, 768, target_resolution)

    response = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample',
        'target_resolution': target_resolution, 'method': 'lanczos_only',
        'preserve_audio': preserve_audio, 'idempotency_key': f'lanczos-{target_resolution}-{preserve_audio}'})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['status'] == 'succeeded', task
    receipt = task['result']
    assert receipt['method'] == 'lanczos_only'
    assert receipt['native_detail'] is False
    assert receipt['target']['width'] == expected['width']
    assert receipt['target']['height'] == expected['height']
    assert receipt['elapsed_seconds'] >= 0
    assert receipt['preserve_audio'] is preserve_audio
    assert receipt['audio_preserved'] is (preserve_audio and True)
    output = backend.resolve_registered_path(receipt['output'])
    assert output.is_file() and output != source
    output_facts = backend.authorized_media_facts(receipt['output'])
    assert (output_facts['width'], output_facts['height']) == (expected['width'], expected['height'])
    assert bool(output_facts['audio_streams']) is preserve_audio

    # The source file, the plan candidate and the adoption record stay unchanged.
    assert source.read_bytes() == source_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert (source.stat().st_size, source.stat().st_mtime_ns) == (source_stat.st_size, source_stat.st_mtime_ns)
    plan_after = client.get('/api/projects/preset-test/plan').json()
    assert plan_after['segments'][0]['video']['path'] == plan_before['segments'][0]['video']['path']
    state = client.get('/api/projects/preset-test/state').json()
    assert state['reviews']['S01']['sample']['status'] == 'approved'
    assert state['reviews']['S01']['sample']['source'] == 'explicit-adoption'
    assert not any(item.get('kind') == 'delivery-upscale' and item.get('status') == 'approved'
                   for item in state.get('artifacts', []))

    manifest = backend.load_project_manifest('preset-test')
    asset = next(item for item in manifest['assets'] if item['id'] == receipt['asset_id'])
    assert asset['kind'] == 'video'
    assert asset['upscale_task_id'] == task['id']
    assert asset['upscale']['native_detail'] is False
    assert backend.resolve_registered_path(asset['sources']['A']) == output
    attempt = client.post('/api/projects/preset-test/upscale-tasks', json={
        'source_asset_id': 'S01', 'source_slot': 'sample',
        'target_resolution': target_resolution, 'method': 'lanczos_only',
        'preserve_audio': preserve_audio, 'idempotency_key': f'lanczos-{target_resolution}-{preserve_audio}'})
    assert attempt.status_code == 200
    assert attempt.json()['id'] == task['id']
    tasks = client.get('/api/projects/preset-test/tasks').json()['tasks']
    assert [item['id'] for item in tasks].count(task['id']) == 1
