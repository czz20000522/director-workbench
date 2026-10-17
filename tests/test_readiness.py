"""Isolated presentation-contract tests; no app startup, files or GPU calls."""
from copy import deepcopy

import pytest

from backend.readiness import build_project_readiness, validation_readiness


STAGE = {'id': 'custom-video-recipe', 'execution': {'mode': 'comfyui'}}


def build(*, validate=None, **overrides):
    args = {'project_id': 'project-a', 'plan': {'revision': 7, 'segments': [{'id': 'S01'}]},
            'stages': [STAGE], 'project_state': {}, 'tasks': [],
            'validate': validate or (lambda stage, asset, values: {'valid': True, 'values': values}),
            'assembly_preflight': lambda: {'ready': True, 'missing_videos': [], 'blocking_joins': []}}
    args.update(overrides)
    return build_project_readiness(**args)


def test_multiple_missing_inputs_are_passed_from_authoritative_validator_once():
    calls = []
    def validate(stage, asset, values):
        calls.append((stage, asset, values))
        return {'valid': False, 'blockers': [
            {'code': 'missing_input', 'message': '请填写提示词', 'input_id': 'prompt', 'action': 'edit_inputs'},
            {'code': 'material_unavailable', 'message': '请上传首帧', 'input_id': 'first-frame', 'action': 'upload_material'},
        ]}
    row = build(validate=validate)['segments'][0]
    assert calls == [('custom-video-recipe', 'S01', {})]
    assert not row['ready']
    assert [reason['input_id'] for reason in row['blockers']] == ['prompt', 'first-frame']
    assert [action['id'] for action in row['next_actions']] == ['edit_inputs', 'upload_material']
    assert row['blockers'][1]['action']['path'] == '/api/projects/project-a/upload'
    assert 'submit' not in {action['id'] for action in row['allowed_actions']}


@pytest.mark.parametrize('stage_id', ['h3-first-native-square', 'motion-first-native-landscape-generation', 'other-model'])
@pytest.mark.parametrize('valid', [True, False])
def test_stage_ids_and_model_ranges_are_not_reimplemented(stage_id, valid):
    validator_result = {'valid': valid, 'h3_duration': {'frames': 209, 'output_seconds': 8.708}}
    if not valid:
        validator_result['blockers'] = [{'code': 'duration_out_of_range', 'input_id': 'duration', 'message': '时长越界'}]
    row = build(stages=[{'id': stage_id}], validate=lambda *args: validator_result)['segments'][0]
    assert row['stage_id'] == stage_id
    assert row['ready'] is valid
    assert row['validation'] == validator_result
    if not valid:
        assert row['blockers'][0]['code'] == 'duration_out_of_range'


def test_draft_and_saved_versions_use_same_validator_without_mutation():
    plan = {'revision': 12, 'segments': [{'id': 'S01', 'prompt': 'saved'}]}
    saved = deepcopy(plan)
    calls = []
    def validate(stage, asset, values):
        calls.append(deepcopy(values))
        values['default'] = 'filled by service'
        return {'valid': True, 'values': values}
    preview = build(plan=plan, validate=validate, drafts={'S01': {'values': {'prompt': 'draft'}}})
    restored = build(plan=plan, validate=validate)
    assert calls == [{'prompt': 'draft'}, {}]
    assert preview['source'] == preview['segments'][0]['source'] == 'draft'
    assert preview['segments'][0]['saved_revision'] == 12
    assert restored['source'] == restored['segments'][0]['source'] == 'saved'
    assert plan == saved


def test_multiple_stages_require_service_selection_and_empty_project_has_next_action():
    def never(*args):
        pytest.fail('must not validate an unselected recipe')
    assert build(plan={'revision': 0, 'segments': []}, validate=never)['next_actions'][0]['id'] == 'add_segment'
    stages = [{'id': 'native-a'}, {'id': 'custom-b'}]
    row = build(stages=stages, validate=never)['segments'][0]
    assert row['blockers'][0]['code'] == 'stage_required'
    assert 'validate' not in {action['id'] for action in row['allowed_actions']}
    selected = build(stages=stages, stage_selector=lambda segment, choices: choices[1]['id'])['segments'][0]
    assert selected['stage_id'] == 'custom-b' and selected['ready']


def test_dependency_reasons_and_unknown_codes_survive_without_localized_parsing():
    result = {'valid': False, 'blockers': [
        {'code': 'dependency_not_ready', 'dependency': 'S00', 'message': '前置任务未完成'},
        {'code': 'future_custom_code', 'message': ''},
    ]}
    row = build(validate=lambda *args: result)['segments'][0]
    assert [item['code'] for item in row['blockers']] == ['dependency_not_ready', 'future_custom_code']
    assert row['blockers'][0]['dependency'] == 'S00'
    assert row['blockers'][1]['message']


@pytest.mark.parametrize(('status', 'expected'), [
    ('scheduler_waiting', 'waiting_gpu'), ('batch_waiting', 'waiting_gpu'),
    ('running', 'running'), ('queued', 'running'), ('submitted', 'running'),
    ('finalizing', 'running'), ('needs_reconcile', 'needs_reconcile'),
])
def test_gpu_waiting_is_not_missing_material_and_active_task_cannot_duplicate(status, expected):
    task = {'id': 't1', 'project_id': 'project-a', 'asset_id': 'S01', 'status': status}
    row = build(tasks=[task])['segments'][0]
    assert row['state'] == expected and not row['ready']
    assert 'submit' not in {action['id'] for action in row['allowed_actions']}
    if status != 'needs_reconcile':
        assert row['blockers'] == []
    else:
        assert row['blockers'][0]['code'] == 'task_needs_reconcile'


def test_readiness_tracks_every_status_that_shared_admission_treats_as_outstanding():
    from backend import task_scheduler
    from backend.readiness import RUNNING_STATUSES, WAITING_STATUSES
    assert task_scheduler.WAITING <= WAITING_STATUSES
    assert task_scheduler.EXECUTING <= RUNNING_STATUSES | {'needs_reconcile'}


def test_external_gpu_busy_allows_admission_and_dependency_waiting_remains_distinct():
    result = build(queue={'state': 'waiting_resource', 'reason': {'code': 'external_queue_busy'}})
    assert result['segments'][0]['ready']
    assert 'submit' in {item['id'] for item in result['segments'][0]['allowed_actions']}
    row = build(tasks=[{'id': 't', 'project_id': 'project-a', 'asset_id': 'S01',
                        'status': 'scheduler_waiting', 'queue': {'state': 'waiting_dependency'}}])['segments'][0]
    assert row['state'] == 'waiting_dependency'


def test_valid_inputs_waiting_only_on_dependencies_can_be_admitted_by_shared_queue():
    validation = {'valid': False, 'blockers': [
        {'code': 'waiting_dependency', 'message': '前置分镜正在执行', 'dependency': 'S00', 'action': 'inspect_dependency'},
    ]}
    row = build(validate=lambda *args: validation)['segments'][0]
    assert not row['ready'] and row['queueable'] and row['state'] == 'waiting_dependency'
    assert 'submit' in {item['id'] for item in row['allowed_actions']}
    assert row['next_actions'][0]['id'] == 'submit'
    assert row['blockers'][0]['action']['method'] == 'GET'
    assert row['blockers'][0]['action']['path'] == '/api/projects/project-a/plan'
    validation['blockers'].append({'code': 'missing_input', 'message': '首帧缺失', 'input_id': 'first-frame'})
    row = build(validate=lambda *args: validation)['segments'][0]
    assert not row['queueable'] and row['state'] == 'blocked'
    assert 'submit' not in {item['id'] for item in row['allowed_actions']}


def test_other_users_tasks_do_not_affect_this_project_and_result_does_not_alias_inputs():
    tasks = [{'id': 'private-task', 'project_id': 'another-users-project', 'asset_id': 'S01', 'status': 'running'}]
    queue = {'state': 'idle'}
    result = build(tasks=tasks, queue=queue)
    assert result['segments'][0]['ready']
    assert 'private-task' not in str(result)
    result['queue']['state'] = 'changed'
    assert queue == {'state': 'idle'}


@pytest.mark.parametrize(('status', 'expected'), [
    (None, 'pending_review'), ('pending_review', 'pending_review'), ('approved', 'adopted'),
    ('changes_requested', 'redo_required'), ('stale', 'review_stale'),
])
def test_success_is_not_automatic_quality_approval(status, expected):
    state = {'reviews': {'S01': {'sample': {'status': status}}}}
    row = build(project_state=state, plan={'revision': 3, 'segments': [{'id': 'S01', 'video': {'path': 'candidate.mp4'}}]})['segments'][0]
    assert row['state'] == expected
    assert row['ready']  # generation eligibility and quality state are separate
    assert row['next_actions'][0]['id'] in {'review', 'redo'}


def test_assembly_collects_all_preflight_blockers_and_uses_real_routes():
    preflight = {'ready': False, 'missing_videos': ['S01'],
                 'blocking_joins': ['S01>S02', 'S02>S03', 'S03>S04'],
                 'joins': [{'key': 'S01>S02', 'status': 'pending'}, {'key': 'S02>S03', 'status': 'stale'},
                           {'key': 'S03>S04', 'status': 'redo_left'}],
                 'sound': {'error': '音轨短于成片'}}
    row = build(assembly_preflight=lambda: preflight)['assembly']
    assert not row['ready']
    assert [item['code'] for item in row['blockers']] == [
        'video_required', 'join_review_required', 'join_review_stale', 'join_redo_required', 'assembly_sound_invalid']
    assert row['blockers'][1]['action']['method'] == 'PUT'
    assert row['blockers'][1]['action']['path'] == '/api/projects/project-a/assembly/joins/S01/S02'
    assert row['blockers'][-1]['action']['path'] == '/api/projects/project-a/assembly/sound'
    assert 'assemble' not in {action['id'] for action in row['allowed_actions']}
    ready = build()['assembly']
    assert ready['allowed_actions'][-1]['path'] == '/api/projects/project-a/assembly'


def test_restart_reconciliation_and_final_review_apply_to_assembly_too():
    task = {'id': 'master-task', 'project_id': 'project-a', 'asset_id': 'CUT', 'status': 'needs_reconcile'}
    row = build(assembly_asset_id='CUT', tasks=[task])['assembly']
    assert row['state'] == 'needs_reconcile' and not row['ready']
    assert row['next_actions'][0]['path'].endswith('/tasks/master-task')
    row = build(plan={'segments': [], 'assembly': {'output': 'old.mp4'}},
                project_state={'reviews': {'MASTER': {'final': {'status': 'approved'}}}})['assembly']
    assert row['state'] == 'adopted'


def test_structured_exceptions_and_invalid_callback_results_fail_closed():
    class ServiceError(Exception):
        detail = {'code': 'upstream_version_stale', 'message': '输入版本已改变', 'dependency': 'S00'}
    def validate(*args):
        raise ServiceError()
    row = build(validate=validate)['segments'][0]
    assert row['blockers'][0]['code'] == 'upstream_version_stale'
    assert not row['ready']
    row = build(validate=lambda *args: None)['segments'][0]
    assert row['blockers'][0]['code'] == 'validation_unavailable'
    assert not row['ready']


def test_readiness_copy_does_not_modify_canonical_validator_result():
    validation = {'valid': False, 'blockers': [{'code': 'missing_input', 'message': '缺提示词', 'action': 'edit_inputs'}]}
    saved = deepcopy(validation)
    row = validation_readiness(validation, project_id='project a', asset_id='S/01', stage_id='custom', revision=2)
    assert validation == saved
    assert row['allowed_actions'][0]['path'] == '/api/projects/project%20a/plan/segments/S%2F01'
