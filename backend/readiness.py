"""Read-only readiness presentation over the workbench's actual validators.

Callbacks must use the same services as submission/preflight. This module does
not inspect files, validate model parameters, submit work, or poll ComfyUI.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable
from urllib.parse import quote


WAITING_STATUSES = {'scheduler_waiting', 'waiting_gpu', 'batch_waiting', 'waiting', 'pending_dispatch'}
RUNNING_STATUSES = {'preparing', 'submitting', 'submitted', 'queued', 'running', 'finalizing', 'stopping', 'stop_requested'}


def _action(action_id: str, label: str, method: str, path: str, **fields: Any) -> dict[str, Any]:
    return {'id': action_id, 'label': label, 'method': method, 'path': path, **fields}


def _blocker(code: str, message: str, action: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {'code': code, 'message': message, 'action': action, **fields}


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for action in actions:
        if action not in result:
            result.append(action)
    return result


def validation_readiness(
    validation: dict[str, Any], *, project_id: str, asset_id: str,
    stage_id: str | None, revision: Any, source: str = 'saved',
) -> dict[str, Any]:
    """Attach stable object/actions to structured validation without revalidating.

    Unknown reason codes are preserved. A legacy/string-only validation failure
    receives a human-readable fallback, never a code inferred from its wording.
    """
    root = f'/api/projects/{quote(project_id, safe="")}'
    edit = _action('edit_inputs', '补齐分镜输入', 'PATCH', f'{root}/plan/segments/{quote(asset_id, safe="")}')
    check = _action('validate', '预检分镜', 'POST', f'{root}/pipeline/{quote(stage_id or "", safe="")}/validate',
                    body={'asset_id': asset_id, 'values': deepcopy(validation.get('values') or {})})
    submit = _action('submit', '加入制作队列', 'POST', f'{root}/tasks',
                     body={'asset_id': asset_id, 'pipeline_stage_id': stage_id,
                           'pipeline_values': deepcopy(validation.get('values') or {})},
                     required_fields=['idempotency_key'])
    blockers = []
    reasons = validation.get('blockers', validation.get('reasons', []))
    if not isinstance(reasons, list):
        reasons = []
    for reason in reasons:
        reason = deepcopy(reason) if isinstance(reason, dict) else {'message': str(reason)}
        code = str(reason.get('code') or 'validation_failed')
        action = reason.pop('action', None)
        if not isinstance(action, dict):
            if action == 'upload_material':
                action = _action('upload_material', '上传所需素材', 'POST', root + '/upload')
            elif action == 'inspect_dependency' or code == 'waiting_dependency':
                action = _action('inspect_dependency', '查看前置分镜与任务', 'GET', root + '/plan')
            else:
                action = dict(edit)
            if reason.get('input_id'):
                action['input_id'] = reason['input_id']
        blockers.append({**reason, 'code': code,
                         'message': str(reason.get('message') or '当前输入暂不能执行，请补齐后重新预检。'),
                         'action': action, 'revision': revision,
                         'object': reason.get('object') or {'kind': 'segment', 'id': asset_id}})
    if validation.get('valid') is not True and not blockers:
        blockers.append(_blocker('validation_failed', str(validation.get('message') or
                                '当前输入暂不能执行，请查看预检并补齐输入。'), edit))
    for blocker in blockers:
        blocker.setdefault('revision', revision)
        blocker.setdefault('object', {'kind': 'segment', 'id': asset_id})
    ready = validation.get('valid') is True and not blockers and stage_id is not None
    # The shared validator can declare valid frozen inputs whose dependencies
    # are still executing. Admission permits these; the scheduler holds them.
    waiting_dependency = bool(stage_id and blockers and
                              all(item['code'] == 'waiting_dependency' for item in blockers))
    queueable = ready or waiting_dependency
    next_actions = _dedupe_actions([item['action'] for item in blockers])
    if queueable:
        next_actions.insert(0, submit)
    return {'asset_id': asset_id, 'stage_id': stage_id, 'revision': revision, 'source': source,
            'object': {'kind': 'segment', 'id': asset_id},
            'state': 'ready' if ready else 'waiting_dependency' if waiting_dependency else 'blocked',
            'ready': ready, 'queueable': queueable, 'blockers': blockers,
            'allowed_actions': _dedupe_actions([edit, check, *([submit] if queueable else []),
                                               *[item['action'] for item in blockers]]),
            'next_actions': next_actions,
            'validation': deepcopy(validation)}


def _call_validation(callback: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        result = callback(*args)
    except Exception as exc:
        # Service HTTP exceptions carry structured detail; no dependency on
        # FastAPI and no interpretation of localized exception strings.
        detail = getattr(exc, 'detail', None)
        if isinstance(detail, dict):
            result = {**deepcopy(detail), 'valid': False}
            if detail.get('code') and not (detail.get('blockers') or detail.get('reasons')):
                result['blockers'] = [deepcopy(detail)]
            return result
        return {'valid': False, 'blockers': [{'code': 'validation_failed',
                                             'message': str(detail or exc) or '预检暂不可用'}]}
    if not isinstance(result, dict):
        return {'valid': False, 'blockers': [{'code': 'validation_unavailable', 'message': '预检未返回有效结果'}]}
    return deepcopy(result)


def _task_for_asset(tasks: list[dict[str, Any]], project_id: str, asset_id: str) -> dict[str, Any] | None:
    # The caller must supply only authorized project tasks. Defense in depth
    # avoids a different user's task influencing this project's actions.
    matches = [task for task in tasks if task.get('project_id') == project_id and
               task.get('asset_id') == asset_id and
               task.get('status') in WAITING_STATUSES | RUNNING_STATUSES | {'needs_reconcile'}]
    return max(matches, key=lambda item: (item.get('created_at') or 0, str(item.get('id', ''))), default=None)


def _apply_task(row: dict[str, Any], task: dict[str, Any] | None, root: str) -> bool:
    if task is None:
        return False
    task_id = str(task.get('id', ''))
    task_path = f'{root}/tasks/{quote(task_id, safe="")}'
    inspect = _action('inspect_task', '查看任务回执', 'GET', task_path)
    status = task.get('status')
    row['task_id'] = task_id
    row['task_status'] = status
    row['allowed_actions'] = [item for item in row['allowed_actions'] if item['id'] not in {'submit', 'assemble'}]
    row['ready'] = False
    row['queueable'] = False
    if status == 'needs_reconcile':
        action = _action('reconcile', '核对重启后的任务回执', 'POST', task_path + '/reconcile')
        row['state'] = 'needs_reconcile'
        row['blockers'].append(_blocker('task_needs_reconcile', '已有任务需要核对回执，暂不能重复提交。', action))
    else:
        action = _action('stop', '停止任务', 'POST', task_path + '/stop')
        queue_state = (task.get('queue') or {}).get('state')
        row['state'] = ('waiting_dependency' if queue_state == 'waiting_dependency' else 'waiting_gpu') if status in WAITING_STATUSES else 'running'
        # Waiting is an execution state, not a missing-input blocker.
    row['allowed_actions'] += [inspect, action]
    row['next_actions'] = [inspect]
    return True


def _review_state(row: dict[str, Any], review: dict[str, Any], has_output: bool, root: str) -> None:
    if not has_output:
        return
    review_action = _action('review', '审核候选', 'POST', root + '/reviews', body={'asset_id': row['asset_id']},
                            required_fields=['stage', 'status', 'expected_revision'])
    redo = next((item for item in row['allowed_actions'] if item['id'] in {'submit', 'assemble'}), None)
    if redo:
        redo = {**redo, 'id': 'redo', 'label': '修改后重做'}
    row['allowed_actions'].append(review_action)
    status = review.get('status')
    if status == 'approved':
        row['state'] = 'adopted'
        row['next_actions'] = [review_action]
    elif status in {'changes_requested', 'stale'}:
        row['state'] = 'redo_required' if status == 'changes_requested' else 'review_stale'
        row['next_actions'] = [redo] if redo else [review_action]
    else:
        row['state'] = 'pending_review'
        adopt = _action('adopt', '审核并采用候选', 'POST', root + '/adoptions', body={'asset_id': row['asset_id']},
                        required_fields=['stage', 'candidate_ref', 'expected_review_revision',
                                         'expected_plan_revision', 'note', 'confirm_adoption'])
        row['allowed_actions'].append(adopt)
        row['next_actions'] = [review_action]
    if redo:
        row['allowed_actions'].append(redo)


def build_project_readiness(
    *, project_id: str, plan: dict[str, Any], stages: list[dict[str, Any]],
    project_state: dict[str, Any], tasks: list[dict[str, Any]],
    validate: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    assembly_preflight: Callable[[], dict[str, Any]] | None = None,
    stage_selector: Callable[[dict[str, Any], list[dict[str, Any]]], str | None] | None = None,
    queue: dict[str, Any] | None = None, drafts: dict[str, dict[str, Any]] | None = None,
    assembly_asset_id: str = 'MASTER',
) -> dict[str, Any]:
    """Build authorized project's readiness, exclusively using shared callbacks.

    ``drafts`` is keyed by segment id: {stage_id?, values:{...}}. It affects
    preview rows only and never changes the saved plan/revision. Stage choice
    comes from the app's execution resolver rather than recipe id assumptions.
    Queue metadata supplied here must already be sanitized for this user.
    """
    root = f'/api/projects/{quote(project_id, safe="")}'
    revision = plan.get('revision', 0)
    rows = []
    known_stages = {str(stage['id']) for stage in stages if stage.get('id')}
    for segment in plan.get('segments', []):
        if not isinstance(segment, dict) or not segment.get('id'):
            continue
        asset_id = str(segment['id'])
        draft = (drafts or {}).get(asset_id)
        source = 'draft' if draft is not None else 'saved'
        stage_id = (draft or {}).get('stage_id')
        if not stage_id:
            stage_id = stage_selector(segment, stages) if stage_selector else (
                segment.get('pipeline_stage_id') or segment.get('stage_id') or
                (stages[0].get('id') if len(stages) == 1 else None))
        values = deepcopy((draft or {}).get('values') or {})
        if stage_id not in known_stages:
            choose = _action('choose_stage', '选择制作方案', 'GET', root + '/pipeline')
            result = {'valid': False, 'blockers': [_blocker('stage_required', '请先选择当前作品的制作方案。', choose)]}
            stage_id = None
        else:
            result = _call_validation(validate, str(stage_id), asset_id, values)
        row = validation_readiness(result, project_id=project_id, asset_id=asset_id,
                                  stage_id=stage_id, revision=revision, source=source)
        row['object'] = {'kind': 'segment', 'id': asset_id}
        row['saved_revision'] = revision
        if stage_id is None:
            row['allowed_actions'] = [item for item in row['allowed_actions'] if item['id'] != 'validate']
        if not _apply_task(row, _task_for_asset(tasks, project_id, asset_id), root):
            reviews = (project_state.get('reviews') or {}).get(asset_id) or {}
            _review_state(row, reviews.get('finish') or reviews.get('sample') or {},
                          bool((segment.get('video') or {}).get('path')), root)
        for blocker in row['blockers']:
            blocker.setdefault('revision', revision)
            blocker.setdefault('object', deepcopy(row['object']))
        rows.append(row)

    assembly = {'asset_id': assembly_asset_id, 'revision': revision, 'source': 'saved',
                'object': {'kind': 'assembly', 'id': assembly_asset_id}, 'ready': False,
                'state': 'blocked', 'blockers': [], 'allowed_actions': [], 'next_actions': []}
    check = _action('preflight_assembly', '预检成片装配', 'GET', root + '/assembly/preflight')
    assemble = _action('assemble', '装配成片', 'POST', root + '/assembly',
                       body={'expected_revision': revision}, required_fields=['idempotency_key'])
    assembly['allowed_actions'] = [check]
    if assembly_preflight:
        preflight = _call_validation(assembly_preflight)
        assembly['preflight'] = preflight
        for asset_id in preflight.get('missing_videos', []):
            edit = _action('edit_inputs', '准备分镜视频', 'PATCH', f'{root}/plan/segments/{quote(str(asset_id), safe="")}')
            assembly['blockers'].append(_blocker('video_required', '分镜尚无可装配视频。', edit, input_id='video', dependency=asset_id))
        joins = {item.get('key'): item for item in preflight.get('joins', []) if isinstance(item, dict)}
        for key in preflight.get('blocking_joins', []):
            join = joins.get(key, {})
            status = join.get('status')
            code = 'join_review_stale' if status == 'stale' else 'join_redo_required' if status in {'redo_left', 'redo_right'} else 'join_review_required'
            left_id, right_id = join.get('left_id'), join.get('right_id')
            if left_id is None or right_id is None:
                left_id, _, right_id = str(key).partition('>')
            action = _action('review_join', '审核接点', 'PUT', root + '/assembly/joins/' +
                             quote(str(left_id), safe='') + '/' + quote(str(right_id), safe=''))
            assembly['blockers'].append(_blocker(code, '接点需要审核或重做后才能装配。', action, dependency=key))
        if (preflight.get('sound') or {}).get('error'):
            sound = _action('set_assembly_sound', '补齐完整音轨', 'PUT', root + '/assembly/sound')
            assembly['blockers'].append(_blocker('assembly_sound_invalid', str(preflight['sound']['error']),
                                                 sound, input_id='audio_asset_id'))
        for blocker in preflight.get('blockers', []):
            if isinstance(blocker, dict):
                assembly['blockers'].append({**deepcopy(blocker), 'action': blocker.get('action') or check})
        if preflight.get('ready') is not True and not assembly['blockers']:
            assembly['blockers'].append(_blocker('assembly_not_ready', str(preflight.get('message') or '成片装配尚未通过预检。'), check))
        assembly['ready'] = preflight.get('ready') is True and not assembly['blockers']
    else:
        assembly['blockers'].append(_blocker('assembly_preflight_unavailable', '请先预检成片装配。', check))
    if assembly['ready']:
        assembly['state'] = 'ready'
        assembly['allowed_actions'].append(assemble)
        assembly['next_actions'] = [assemble]
    else:
        assembly['next_actions'] = _dedupe_actions([item['action'] for item in assembly['blockers']])
    if not _apply_task(assembly, _task_for_asset(tasks, project_id, assembly_asset_id), root):
        _review_state(assembly, ((project_state.get('reviews') or {}).get(assembly_asset_id) or {}).get('final') or {},
                      bool((plan.get('assembly') or {}).get('output')), root)
    for blocker in assembly['blockers']:
        blocker.setdefault('revision', revision)
        blocker.setdefault('object', deepcopy(assembly['object']))
    next_actions = _dedupe_actions([action for row in rows for action in row['next_actions']] + assembly['next_actions'])
    if not rows:
        add = _action('add_segment', '创建首个分镜', 'POST', root + '/plan/segments')
        next_actions = [add]
    return {'schema_version': 1, 'project_id': project_id, 'revision': revision,
            'source': 'draft' if drafts else 'saved', 'segments': rows, 'assembly': assembly,
            'queue': deepcopy(queue or {}), 'next_actions': next_actions}
