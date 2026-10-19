from backend import app as backend


def review_for(path, *, plan_revision=3, task_id='task-1', segment_signature=None):
    stat = path.stat()
    return {'status': 'approved', 'source': 'explicit-adoption', 'candidate_ref': str(path),
            'adoption_binding': {'task_id': task_id, 'version_task_id': task_id,
                                 'plan_revision': plan_revision,
                                 'segment_signature': segment_signature,
                                 'file_identity': {'path': str(path), 'size_bytes': stat.st_size,
                                                   'modified_ns': stat.st_mtime_ns}}}


def test_assembly_blocks_unadopted_segment_without_creating_work(monkeypatch, tmp_path):
    candidate = tmp_path / 'S01.mp4'
    candidate.write_bytes(b'candidate')
    document = {'revision': 3, 'segments': [{'id': 'S01', 'video': {'path': str(candidate)},
                                             'current_version_task_id': 'task-1'}]}
    monkeypatch.setattr(backend, 'resolve_registered_path', lambda value: candidate if str(value) == str(candidate) else candidate)
    monkeypatch.setattr(backend, 'build_project_state', lambda project_id: {'reviews': {}})
    blockers = backend.assembly_adoption_blockers({'id': 'project-a'}, document)
    assert blockers[0]['code'] == 'adoption_required'


def test_assembly_accepts_current_explicit_adoption_and_rejects_changed_file(monkeypatch, tmp_path):
    candidate = tmp_path / 'S01.mp4'
    candidate.write_bytes(b'candidate')
    document = {'revision': 3, 'segments': [{'id': 'S01', 'video': {'path': str(candidate)},
                                             'current_version_task_id': 'task-1'}]}
    monkeypatch.setattr(backend, 'resolve_registered_path', lambda value: candidate if str(value) == str(candidate) else candidate)
    state = {'reviews': {'S01': {'sample': review_for(candidate, segment_signature=backend.adoption_segment_signature(document, 'S01'))}}}
    monkeypatch.setattr(backend, 'build_project_state', lambda project_id: state)
    assert backend.assembly_adoption_blockers({'id': 'project-a'}, document) == []
    # Adding an unrelated shot bumps the plan revision but must not invalidate S01.
    document['revision'] = 4
    document['segments'].append({'id': 'S02', 'video': {'path': str(tmp_path / 'S02.mp4')}, 'current_version_task_id': 'task-2'})
    assert all(blocker['dependency'] != 'S01' for blocker in backend.assembly_adoption_blockers({'id': 'project-a'}, document))
    # Changing the adopted segment's own duration must invalidate it.
    document['segments'][0]['duration_seconds'] = 7
    assert backend.assembly_adoption_blockers({'id': 'project-a'}, document)[0]['code'] == 'adoption_plan_stale'
    document['segments'][0]['duration_seconds'] = None
    candidate.write_bytes(b'changed candidate')
    assert backend.assembly_adoption_blockers({'id': 'project-a'}, document)[0]['code'] == 'adoption_candidate_changed'
