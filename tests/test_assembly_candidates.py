from urllib.parse import quote
import json
import av

import pytest
from backend import app as backend


def test_assembly_records_actual_duration_not_old_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', tmp_path)
    video = tmp_path / 'new.mp4'
    with av.open(str(video), 'w') as container:
        stream = container.add_stream('mpeg4', rate=24)
        stream.width, stream.height, stream.pix_fmt = 32, 48, 'yuv420p'
        for _ in range(48):
            frame = av.VideoFrame(32, 48, 'yuv420p')
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'segments': [], 'assembly': {'duration_seconds': 999}}), encoding='utf-8')
    backend.record_assembly_result('new-task', 'workflow.json', {'outputs': ['new.mp4']}, plan)
    recorded = json.loads(plan.read_text(encoding='utf-8'))['assembly']
    assert recorded['duration_seconds'] == pytest.approx(2, abs=1/24)
    assert recorded['status'] == 'candidate_generated'
    backend.record_assembly_result('missing-task', 'workflow.json', {'outputs': ['missing.mp4']}, plan)
    assert json.loads(plan.read_text(encoding='utf-8'))['assembly']['duration_seconds'] is None
    assert backend.assembly_duration('../outside.mp4') is None


def test_adopted_candidate_is_bound_and_video_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'PROJECT', tmp_path)
    monkeypatch.setattr(backend, 'INPUT_ROOT', tmp_path / 'input')
    candidate = tmp_path / 'adopted B.mp4'
    candidate.write_bytes(b'approved video')
    document = {'segments': [{'id': 'S01', 'video': {'path': 'old.mp4'}}]}
    state = {'reviews': {'S01': {'sample': {'status': 'approved', 'candidate_ref': '/media-file?path=' + quote(str(candidate))}}}}
    backend.bind_approved_assembly_candidates(document, state, False)
    assert document['segments'][0]['video']['path'] == str(candidate)
    graph = {'video': {'class_type': 'LoadVideo', 'inputs': {'file': str(candidate)}}}
    staged = backend.stage_comfy_input_files(graph, 'project')
    candidate.write_bytes(b'later replacement')
    assert (backend.INPUT_ROOT / graph['video']['inputs']['file']).read_bytes() == b'approved video'
    assert staged[0]['source'] == str(candidate)


def test_finish_review_wins_and_missing_adopted_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'PROJECT', tmp_path)
    sample = tmp_path / 'sample.mp4'
    sample.write_bytes(b'sample')
    document = {'segments': [{'id': 'S01', 'video': {'path': str(sample)}}]}
    state = {'reviews': {'S01': {
        'sample': {'status': 'approved', 'candidate_ref': str(sample)},
        'finish': {'status': 'approved', 'candidate_ref': str(tmp_path / 'missing.mp4')},
    }}}
    with pytest.raises(ValueError, match='已采用的视频不存在'):
        backend.bind_approved_assembly_candidates(document, state, True)
    assert document['segments'][0]['video']['path'] == str(sample)
