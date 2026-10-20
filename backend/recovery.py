"""Read-only project resume plan facts.

A recovery view answers "where does this work stand and what could the caller
explicitly do next".  It never retries a task, adopts a candidate, rewrites the
plan or repairs a receipt: every write stays behind an explicit endpoint that
the caller must invoke with its own confirmation.

The backend runtime module is injected by the router.  This module imports no
FastAPI app, so it can be unit-tested against a synthetic runtime object and it
cannot create an import cycle with ``backend.app``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 1

# A task in one of these states is still being driven by the scheduler.  The
# resume view calls that family ``generating``; it does not collapse waiting and
# running into a promise that work is progressing.
WAITING_STATUSES = frozenset({
    'queued', 'scheduler_waiting', 'batch_waiting', 'waiting_gpu', 'waiting',
    'pending_dispatch',
})
RUNNING_STATUSES = frozenset({
    'preparing', 'dispatching', 'submitting', 'submitted', 'running',
    'finalizing', 'stop_requested', 'stopping',
})
ACTIVE_STATUSES = WAITING_STATUSES | RUNNING_STATUSES
NEEDS_RECONCILE = 'needs_reconcile'
SEGMENT_STATES = ('planned', 'generating', 'video_generated', 'adopted',
                  'needs_reconcile', 'failed', 'stopped')
SAMPLE_STAGES = ('finish', 'sample')


def _root(project_id: str) -> str:
    return f'/api/projects/{quote(project_id, safe="")}'


def _action(action_id: str, label: str, method: str, path: str, *,
            body: dict[str, Any] | None = None,
            required_fields: list[str] | None = None) -> dict[str, Any]:
    """Describe an explicit endpoint; nothing here calls or queues it."""
    action: dict[str, Any] = {'id': action_id, 'label': label, 'method': method, 'path': path}
    if body is not None:
        action['body'] = body
    if required_fields is not None:
        action['required_fields'] = required_fields
    return action


def _dedupe(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for action in actions:
        if action not in result:
            result.append(action)
    return result


def _project_tasks(backend: Any, project_id: str) -> list[dict[str, Any]]:
    """Read the project's task rows newest-first; no task is mutated."""
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(
            'SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC, id DESC',
            (project_id,),
        ).fetchall()
    return [backend.task_row(row) for row in rows]


def _task_asset_id(task: dict[str, Any]) -> str:
    value = task.get('asset_id')
    if value:
        return str(value)
    payload = task.get('payload')
    if isinstance(payload, dict):
        return str(payload.get('asset_id') or '')
    return ''


def _latest_tasks_by_asset(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for task in tasks:
        asset_id = _task_asset_id(task)
        if asset_id:
            latest.setdefault(asset_id, task)
    return latest


def _latest_active_task(tasks: list[dict[str, Any]], asset_id: str) -> dict[str, Any] | None:
    """Newest task that still affects readiness (active or awaiting reconcile)."""
    for task in tasks:
        if _task_asset_id(task) != asset_id:
            continue
        if task.get('status') in ACTIVE_STATUSES or task.get('status') == NEEDS_RECONCILE:
            return task
    return None


def _review_for(reviews: dict[str, Any], asset_id: str,
                stages: tuple[str, ...] = SAMPLE_STAGES) -> dict[str, Any] | None:
    records = reviews.get(asset_id) if isinstance(reviews, dict) else None
    if not isinstance(records, dict):
        return None
    for stage in stages:
        review = records.get(stage)
        if isinstance(review, dict):
            return review
    return None


def _is_explicit_adoption(review: dict[str, Any] | None) -> bool:
    return bool(review and review.get('status') == 'approved'
                and review.get('source') == 'explicit-adoption'
                and isinstance(review.get('adoption_binding'), dict))


def _video_path(segment: dict[str, Any]) -> str | None:
    video = segment.get('video')
    if not isinstance(video, dict):
        return None
    path = video.get('path')
    return str(path) if path else None


def _path_exists(backend: Any, value: str | None) -> bool:
    if not value:
        return False
    try:
        return backend.resolve_registered_path(value).is_file()
    except Exception:  # AccessDenied/HTTPException/OSError/ValueError are all "not a readable fact".
        return False


def _view_task_action(project_id: str, task_id: str) -> dict[str, Any]:
    return _action('inspect_task', '查看任务回执', 'GET',
                   f'{_root(project_id)}/tasks/{quote(task_id, safe="")}')


def _stop_task_action(project_id: str, task_id: str) -> dict[str, Any]:
    return _action('stop_task', '请求停止任务', 'POST',
                   f'{_root(project_id)}/tasks/{quote(task_id, safe="")}/stop')


def _reconcile_task_action(project_id: str, task_id: str) -> dict[str, Any]:
    return _action('reconcile_task', '核对原任务回执（不重试）', 'POST',
                   f'{_root(project_id)}/tasks/{quote(task_id, safe="")}/reconcile')


def _submit_action(project_id: str, asset_id: str) -> dict[str, Any]:
    return _action('submit_generation', '显式提交生成任务', 'POST',
                   f'{_root(project_id)}/tasks', body={'asset_id': asset_id},
                   required_fields=['idempotency_key', 'pipeline_stage_id', 'pipeline_values'])


def _adopt_action(project_id: str, asset_id: str) -> dict[str, Any]:
    return _action('adopt_candidate', '审核并明确采用候选', 'POST',
                   f'{_root(project_id)}/adoptions', body={'asset_id': asset_id},
                   required_fields=['stage', 'candidate_ref', 'expected_review_revision',
                                    'expected_plan_revision', 'note', 'confirm_adoption'])


def _review_action(project_id: str, asset_id: str) -> dict[str, Any]:
    return _action('review_candidate', '查看并审核候选', 'POST',
                   f'{_root(project_id)}/reviews', body={'asset_id': asset_id},
                   required_fields=['stage', 'status', 'expected_revision'])


def _plan_action(project_id: str) -> dict[str, Any]:
    return _action('read_plan', '查看当前计划', 'GET', f'{_root(project_id)}/plan')


def _preflight_action(project_id: str) -> dict[str, Any]:
    return _action('preflight_assembly', '预检成片装配', 'GET',
                   f'{_root(project_id)}/assembly/preflight')


def _assemble_action(project_id: str, revision: Any) -> dict[str, Any]:
    return _action('assemble', '显式装配成片', 'POST', f'{_root(project_id)}/assembly',
                   body={'expected_revision': revision}, required_fields=['idempotency_key'])


def _review_join_action(project_id: str, key: str) -> dict[str, Any]:
    left, _, right = str(key).partition('>')
    return _action('review_join', '审核接点', 'PUT',
                   f'{_root(project_id)}/assembly/joins/{quote(left, safe="")}/{quote(right, safe="")}')


def _sound_action(project_id: str) -> dict[str, Any]:
    return _action('set_assembly_sound', '补齐完整音轨', 'PUT',
                   f'{_root(project_id)}/assembly/sound')


def _add_segment_action(project_id: str) -> dict[str, Any]:
    return _action('add_segment', '创建首个分镜', 'POST',
                   f'{_root(project_id)}/plan/segments',
                   required_fields=['segment_id', 'duration_seconds'])


def _segment_actions(project_id: str, asset_id: str, state: str,
                     task_id: str | None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if state == 'generating':
        if task_id:
            actions.extend((_view_task_action(project_id, task_id),
                            _stop_task_action(project_id, task_id)))
    elif state == 'needs_reconcile':
        if task_id:
            actions.extend((_view_task_action(project_id, task_id),
                            _reconcile_task_action(project_id, task_id)))
    elif state == 'failed':
        if task_id:
            actions.append(_view_task_action(project_id, task_id))
        actions.append(_submit_action(project_id, asset_id))
    elif state == 'stopped':
        if task_id:
            actions.append(_view_task_action(project_id, task_id))
        actions.append(_submit_action(project_id, asset_id))
    elif state == 'video_generated':
        actions.extend((_review_action(project_id, asset_id),
                        _adopt_action(project_id, asset_id)))
    elif state == 'planned':
        actions.append(_submit_action(project_id, asset_id))
    return actions


def _segment_state(plan_status: str | None, task_status: str | None,
                   adopted: bool, has_video: bool) -> str:
    if task_status == NEEDS_RECONCILE or plan_status == NEEDS_RECONCILE:
        return 'needs_reconcile'
    if task_status in ACTIVE_STATUSES:
        return 'generating'
    if task_status == 'failed':
        return 'failed'
    if task_status == 'stopped':
        return 'stopped'
    if adopted:
        return 'adopted'
    if task_status == 'succeeded' or plan_status in {'video_generated', 'adopted'} or has_video:
        return 'video_generated'
    return 'planned'


def _segment_facts(backend: Any, project_id: str, segment: dict[str, Any],
                   task: dict[str, Any] | None, review: dict[str, Any] | None) -> dict[str, Any]:
    asset_id = str(segment.get('id'))
    video = _video_path(segment)
    adopted = _is_explicit_adoption(review)
    plan_status = segment.get('status') if isinstance(segment.get('status'), str) else None
    task_status = task.get('status') if task and isinstance(task.get('status'), str) else None
    state = _segment_state(plan_status, task_status, adopted, _path_exists(backend, video))
    task_id = None
    if task and task.get('id'):
        task_id = str(task['id'])
    elif segment.get('current_version_task_id'):
        task_id = str(segment['current_version_task_id'])
    elif segment.get('comfyui_task_id'):
        task_id = str(segment['comfyui_task_id'])
    review_candidate = review.get('candidate_ref') if isinstance(review, dict) else None
    alternate = (segment.get('video') or {}) if isinstance(segment.get('video'), dict) else {}
    alternate = alternate.get('rework_candidate') or alternate.get('finish_review') or {}
    candidate_ref = review_candidate or video or (str(alternate['path']) if alternate.get('path') else None)
    return {
        'id': asset_id,
        'state': state,
        'task_id': task_id,
        'task_status': task_status,
        'plan_status': plan_status,
        'candidate_ref': str(candidate_ref) if candidate_ref else None,
        'adopted': adopted,
        'video': {'path': video, 'exists': _path_exists(backend, video)},
        'next_actions': _segment_actions(project_id, asset_id, state, task_id),
    }


def _assembly_blockers(backend: Any, project_id: str, manifest: dict[str, Any],
                       document: dict[str, Any], tasks: list[dict[str, Any]],
                       assembly_asset_id: str) -> list[dict[str, Any]]:
    segments = [item for item in document.get('segments', []) if isinstance(item, dict) and item.get('id')]
    blockers: list[dict[str, Any]] = []
    builder = backend.assembly_builder_blocker(manifest)
    if isinstance(builder, dict):
        blockers.append(dict(builder))
    if not segments:
        blockers.append({'code': 'segments_required', 'message': '装配需要至少一个分镜。',
                         'action': _add_segment_action(project_id)})
        return blockers
    video_blockers = backend.assembly_video_blockers(document)
    for blocker in video_blockers:
        blockers.append({**dict(blocker), 'action': _plan_action(project_id)})
    try:
        adoption_blockers = backend.assembly_adoption_blockers(manifest, document)
    except Exception as exc:  # A read-only view must report, not raise, an unavailable check.
        blockers.append({'code': 'assembly_check_unavailable',
                         'message': f'无法核对分镜采用状态: {exc}',
                         'action': _plan_action(project_id)})
    else:
        for blocker in adoption_blockers:
            if not isinstance(blocker, dict):
                continue
            dependency = str(blocker.get('dependency') or
                             ((blocker.get('object') or {}).get('id') if isinstance(blocker.get('object'), dict) else '') or '')
            action = _adopt_action(project_id, dependency) if dependency else _plan_action(project_id)
            blockers.append({**dict(blocker), 'action': action})
    for key in backend.assembly_edit.blocking_joins(document):
        blockers.append({'code': 'join_review_required', 'message': '接点需要审核后才能装配。',
                         'dependency': key, 'action': _review_join_action(project_id, str(key))})
    try:
        backend.assembly_sound_selection(manifest, document)
    except ValueError as exc:
        blockers.append({'code': 'assembly_sound_invalid', 'message': str(exc),
                         'action': _sound_action(project_id)})
    active = _latest_active_task(tasks, assembly_asset_id)
    if active:
        task_id = str(active.get('id') or '')
        if active.get('status') == NEEDS_RECONCILE:
            blockers.append({'code': 'assembly_needs_reconcile',
                             'message': '已有装配任务需要核对回执后才能重新装配。',
                             'task_id': task_id,
                             'action': _reconcile_task_action(project_id, task_id)})
        else:
            blockers.append({'code': 'assembly_task_in_progress',
                             'message': '已有装配任务在执行；先查看或停止它，不要重复提交。',
                             'task_id': task_id,
                             'action': _view_task_action(project_id, task_id)})
    return blockers


def _adopt_assembly_action(project_id: str, candidate_ref: str, plan_revision: int,
                           review_revision: int) -> dict[str, Any]:
    return _action('adopt_assembly_candidate', '显式采用成片候选', 'POST',
                   f'{_root(project_id)}/adoptions', body={
                       'asset_id': 'MASTER', 'stage': 'final',
                       'candidate_ref': candidate_ref,
                       'expected_review_revision': review_revision,
                       'expected_plan_revision': plan_revision,
                       'note': '恢复计划提示：显式复核并采用当前成片候选。',
                       'confirm_adoption': True,
                   }, required_fields=['note', 'candidate_ref'])


def _assembly_facts(backend: Any, project_id: str, manifest: dict[str, Any],
                    document: dict[str, Any], reviews: dict[str, Any],
                    tasks: list[dict[str, Any]]) -> dict[str, Any]:
    assembly_asset_id = str(manifest.get('assembly_asset_id') or 'MASTER')
    assembly_document = document.get('assembly') if isinstance(document.get('assembly'), dict) else {}
    active = _latest_active_task(tasks, assembly_asset_id)
    blockers = _assembly_blockers(backend, project_id, manifest, document, tasks, assembly_asset_id)
    final_review = _review_for(reviews, assembly_asset_id, ('final',))
    adopted = _is_explicit_adoption(final_review)
    candidate = assembly_document.get('output') or (final_review or {}).get('candidate_ref')
    ready = not blockers
    if active and active.get('status') == NEEDS_RECONCILE:
        state = 'needs_reconcile'
    elif active:
        state = 'generating'
    elif ready:
        state = 'ready'
    else:
        state = 'blocked'
    next_actions: list[dict[str, Any]] = []
    if ready:
        if candidate and not adopted:
            next_actions = [_preflight_action(project_id),
                            _adopt_assembly_action(project_id, str(candidate),
                                                   int(document.get('revision', 0)),
                                                   int((final_review or {}).get('revision', 0)))]
        else:
            next_actions = [_preflight_action(project_id), _assemble_action(project_id, document.get('revision', 0))]
    else:
        next_actions = _dedupe([blocker.get('action') for blocker in blockers if isinstance(blocker.get('action'), dict)])
        if not next_actions:
            next_actions = [_preflight_action(project_id)]
    if active:
        task_id = str(active.get('id') or '')
        next_actions = _dedupe([_view_task_action(project_id, task_id), *next_actions])
    try:
        sound = backend.assembly_sound_selection(manifest, document)
    except ValueError:
        sound = None
    return {
        'asset_id': assembly_asset_id,
        'state': state,
        'ready': ready,
        'revision': document.get('revision', 0),
        'blockers': blockers,
        'candidate_ref': str(candidate) if candidate else None,
        'adopted': adopted,
        'output': assembly_document.get('output'),
        'output_status': assembly_document.get('status'),
        'task_id': str(active['id']) if active and active.get('id') else None,
        'task_status': active.get('status') if active else None,
        'sound': sound,
        # This endpoint never probes media for inter-shot continuity.  The real
        # preflight owns that check; saying so keeps the resume view honest.
        'continuity': {'checked': False,
                       'reason': '恢复计划不执行连续性媒体探测；请以装配预检的 continuity 结果为准。'},
        'next_actions': next_actions,
    }


def build_resume_plan(backend: Any, project_id: str) -> dict[str, Any]:
    """Build the read-only resume plan from project, plan, tasks and records."""
    manifest = backend.require_project_manifest(project_id)
    document, _plan_path = backend.load_project_plan(project_id)
    state = backend.build_project_state(project_id)
    reviews = state.get('reviews') if isinstance(state.get('reviews'), dict) else {}
    tasks = _project_tasks(backend, project_id)
    latest = _latest_tasks_by_asset(tasks)
    segments = []
    for segment in document.get('segments', []):
        if not isinstance(segment, dict) or not segment.get('id'):
            continue
        asset_id = str(segment['id'])
        segments.append(_segment_facts(backend, project_id, segment,
                                       latest.get(asset_id), _review_for(reviews, asset_id)))
    assembly = _assembly_facts(backend, project_id, manifest, document, reviews, tasks)
    actions = _dedupe([action for row in segments for action in row['next_actions']]
                      + list(assembly['next_actions']))
    if not segments:
        actions = _dedupe([_add_segment_action(project_id), *actions])
    counts: dict[str, int] = {}
    for row in segments:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'read_only': True,
        'project': {
            'id': str(manifest.get('id') or project_id),
            'title': manifest.get('title'),
            'series': manifest.get('series'),
        },
        'plan': {
            'revision': document.get('revision', 0),
            'status': document.get('status'),
            'segment_count': len(segments),
        },
        'segments': segments,
        'segment_states': counts,
        'assembly': assembly,
        'next_explicit_actions': actions,
    }
