"""Explicit last-frame carry contract tests (Issue #122 cross-shot continuity).

These tests pin the observable contract:

* only an explicitly adopted source candidate can be carried;
* source and target must be two different shots of the same project;
* the last frame becomes a new PNG asset and the target first-frame reference;
* repeating the same request is idempotent (no second extraction, no revision
  bump, no duplicate asset);
* missing FFmpeg and un-adopted sources are structured blockers;
* no generation task, adoption or assembly is created.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend import app as backend
from backend import continuity_transfer
from backend.media_operations import FFMPEG
from backend.routers.continuity_transfer import ROUTER

PROJECT_ID = 'preset-test'
BASE = f'/api/projects/{PROJECT_ID}'
CAPABILITY_TEMPLATE = '/api/projects/{project_id}/segments/{segment_id}/continuity-capability'
CARRY_TEMPLATE = '/api/projects/{project_id}/segments/{segment_id}/carry-frame'
CAPABILITY = BASE + '/segments/{segment_id}/continuity-capability'
CARRY = BASE + '/segments/{segment_id}/carry-frame'
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'fixture-png-body'


@pytest.fixture
def continuity_client(preset_client):
    client, project = preset_client
    paths = set(backend.app.openapi().get('paths', {}))
    if not {CARRY_TEMPLATE, CAPABILITY_TEMPLATE} <= paths:
        backend.app.include_router(ROUTER)
    return client, project


def _write_plan(project: Path, source: Path) -> dict:
    workspace = project / 'workspaces' / PROJECT_ID
    workspace.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'fixture-source-video')
    document, plan_path = backend.load_project_plan(PROJECT_ID)
    document.update({
        'revision': 1,
        'segments': [
            {
                'id': 'S01', 'start_seconds': 0, 'duration_seconds': 5,
                'status': 'video_generated', 'current_version_task_id': 'task-v1',
                'comfyui_task_id': 'prompt-1', 'video': {'path': str(source)},
            },
            {
                'id': 'S02', 'start_seconds': 5, 'duration_seconds': 4,
                'status': 'planned',
            },
        ],
        'assembly': None,
    })
    plan_path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    return document


def _adopt_sample(client, source: Path) -> None:
    response = client.post(f'{BASE}/reviews', json={
        'asset_id': 'S01', 'stage': 'sample', 'status': 'approved',
        'note': '测试用明确采用记录', 'candidate_ref': str(source),
        'expected_revision': 0, 'expected_plan_revision': 1,
        'source': 'explicit-adoption', 'adopted_variant': 'A',
    })
    assert response.status_code == 200, response.text


def _facts(path) -> dict:
    return {'known': True, 'width': 160, 'height': 96, 'duration_seconds': 5.0,
            'fps': 12.0, 'frames': 60, 'audio_streams': 1}


def _stub_extraction(monkeypatch, calls: list[tuple[Path, Path]]):
    def extract(source: Path, output: Path) -> dict:
        calls.append((Path(source), Path(output)))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PNG_BYTES)
        return {'output': output, 'elapsed_seconds': 0.0, 'command': []}

    monkeypatch.setattr(continuity_transfer, '_extract_last_frame', extract)
    return extract


def _carry(client, *, source='S01', slot='sample', target='S02'):
    return client.post(CARRY.format(segment_id=target), json={
        'source_segment_id': source, 'source_slot': slot, 'target_slot': 'first_frame'})


def _plan() -> dict:
    document, _ = backend.load_project_plan(PROJECT_ID)
    return document


def test_carry_frame_registers_last_frame_as_target_first_frame(continuity_client, monkeypatch):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(backend, 'authorized_media_facts', _facts)
    calls: list[tuple[Path, Path]] = []
    _stub_extraction(monkeypatch, calls)

    response = _carry(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['created'] is True
    assert body['extracted'] is True
    assert body['already_current'] is False
    assert body['segment_id'] == 'S02'
    assert body['target_slot'] == 'first_frame'
    assert body['source']['segment_id'] == 'S01'
    assert body['source']['slot'] == 'sample'
    assert body['source']['last_frame_seconds'] == 4.917
    assert body['frame']['asset_id'].startswith('CF-')
    assert body['frame']['media_ref'].startswith('asset:')
    assert body['frame']['path'] == body['target_first_frame']
    assert len(calls) == 1

    document = _plan()
    target = next(item for item in document['segments'] if item['id'] == 'S02')
    assert target['keyframes']['first'] == body['frame']['path']
    assert target['continuity_frame']['source_segment_id'] == 'S01'
    assert target['continuity_frame']['source_slot'] == 'sample'
    assert target['continuity_frame']['previous_first_frame'] is None
    assert document['revision'] == body['plan_revision']

    manifest = backend.load_project_manifest(PROJECT_ID)
    asset = next(item for item in manifest['assets'] if item['id'] == body['frame']['asset_id'])
    assert asset['kind'] == 'image'
    assert asset['image_path'] == body['frame']['path']
    assert asset['continuity_frame']['target_segment_id'] == 'S02'
    assert Path(backend.resolve_registered_path(body['frame']['path'])).is_file()

    assert client.get(f'{BASE}/tasks').json()['tasks'] == []
    state = client.get(f'{BASE}/state').json()
    assert state['reviews']['S01']['sample']['source'] == 'explicit-adoption'
    assert 'S02' not in state['reviews']


def test_repeated_carry_is_idempotent(continuity_client, monkeypatch):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(backend, 'authorized_media_facts', _facts)
    calls: list[tuple[Path, Path]] = []
    _stub_extraction(monkeypatch, calls)

    first = _carry(client)
    assert first.status_code == 200, first.text
    plan_after_first = _plan()
    assets_after_first = len(backend.load_project_manifest(PROJECT_ID)['assets'])

    second = _carry(client)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body['created'] is False
    assert body['extracted'] is False
    assert body['already_current'] is True
    assert body['frame'] == first.json()['frame']
    assert body['plan_revision'] == plan_after_first['revision']
    assert len(calls) == 1
    assert len(backend.load_project_manifest(PROJECT_ID)['assets']) == assets_after_first


def test_unadopted_source_is_a_structured_blocker(continuity_client):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    before = _plan()['revision']

    response = _carry(client)
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'source_not_adopted'
    assert detail['retryable'] is True
    assert _plan()['revision'] == before
    assert 'S02' not in (client.get(f'{BASE}/state').json().get('reviews') or {})


def test_unknown_source_segment_is_rejected(continuity_client):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)

    response = _carry(client, source='S99')
    assert response.status_code == 404, response.text
    assert response.json()['detail']['code'] == 'source_segment_not_found'


def test_missing_source_file_is_a_structured_blocker(continuity_client):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    source.unlink()
    before = _plan()['revision']

    response = _carry(client)
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'source_unavailable'
    assert _plan()['revision'] == before


def test_source_and_target_must_be_different_shots(continuity_client):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)

    response = _carry(client, source='S01', target='S01')
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'source_is_target'


def test_missing_ffmpeg_returns_structured_blocker_without_plan_change(continuity_client, monkeypatch):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(backend, 'authorized_media_facts', _facts)
    monkeypatch.setattr(continuity_transfer, 'FFMPEG', Path('missing-ffmpeg.exe'))
    before = _plan()['revision']

    response = _carry(client)
    assert response.status_code == 503, response.text
    detail = response.json()['detail']
    assert detail['code'] == 'continuity_tool_unavailable'
    assert 'ffmpeg_missing' in {item['code'] for item in detail['blockers']}
    assert _plan()['revision'] == before


def test_capability_reports_adopted_sources_and_frame_position(continuity_client, monkeypatch):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(backend, 'authorized_media_facts', _facts)

    response = client.get(CAPABILITY.format(segment_id='S02'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['can_carry'] is True
    assert body['target_segment_id'] == 'S02'
    assert body['target']['current_first_frame'] is None
    assert body['tool']['available'] is True
    rows = {(row['source_segment_id'], row['source_slot']): row for row in body['source_candidates']}
    candidate = rows[('S01', 'sample')]
    assert candidate['available'] is True
    assert candidate['duration_seconds'] == 5.0
    assert candidate['last_frame_seconds'] == 4.917
    assert candidate['frames'] == 60
    assert 'recommended' not in json.dumps(body, ensure_ascii=False)


def test_capability_marks_missing_ffmpeg_as_a_blocker(continuity_client, monkeypatch):
    client, project = continuity_client
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    _adopt_sample(client, source)
    monkeypatch.setattr(backend, 'authorized_media_facts', _facts)
    monkeypatch.setattr(continuity_transfer, 'FFMPEG', Path('missing-ffmpeg.exe'))

    response = client.get(CAPABILITY.format(segment_id='S02'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['can_carry'] is False
    assert 'ffmpeg_missing' in {item['code'] for item in body['blockers']}
    source_row = next(row for row in body['source_candidates'] if row['available'])
    assert source_row['source_segment_id'] == 'S01'
    assert source_row['blockers'] == []


@pytest.mark.host_tool
def test_real_ffmpeg_extracts_last_frame_png(continuity_client):
    client, project = continuity_client
    if not FFMPEG.is_file():
        pytest.skip('local FFmpeg is required for the host-tool fixture')
    source = project / 'workspaces' / PROJECT_ID / 'assets' / 'S01.mp4'
    _write_plan(project, source)
    # Replace the placeholder bytes with a real decodable video.
    command = [str(FFMPEG), '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'lavfi', '-i', 'testsrc=size=160x96:rate=12', '-t', '1',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(source)]
    log = source.with_suffix('.ffmpeg.log')
    with log.open('wb') as handle:
        result = subprocess.run(command, stdout=handle, stderr=handle, timeout=120)
    assert result.returncode == 0, log.read_text(encoding='utf-8', errors='replace')[-800:]
    _adopt_sample(client, source)

    response = _carry(client)
    assert response.status_code == 200, response.text
    body = response.json()
    frame_path = Path(backend.resolve_registered_path(body['frame']['path']))
    assert frame_path.is_file() and frame_path.stat().st_size > 0
    assert frame_path.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'

    second = _carry(client)
    assert second.status_code == 200, second.text
    assert second.json()['created'] is False
    assert second.json()['extracted'] is False
