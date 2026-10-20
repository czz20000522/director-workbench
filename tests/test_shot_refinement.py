"""Explicit shot-refinement contract tests.

These exercise the new router in isolation so the contract can be verified before
the Lead wires it into app.py. Backend project/plan/candidate/record helpers are
replaced with fixture facts; no task, model, ComfyUI or real media is involved.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import app as backend
from backend import refinement
from backend.routers.refinement import ROUTER

PROJECT_ID = 'project-refine'
OTHER_PROJECT = 'project-other'


@pytest.fixture
def refinement_env(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    current = workspace / 'S01-current.mp4'
    rework = workspace / 'S01-rework.mp4'
    history = workspace / 'S01-history.mp4'
    tied = workspace / 'S04-current.mp4'
    missing = workspace / 'S03-missing.mp4'
    foreign = tmp_path / 'foreign.mp4'
    for path in (current, rework, history, tied, foreign):
        path.write_bytes(b'fixture-video')

    plan = {
        'revision': 7,
        'segments': [
            {
                'id': 'S01', 'status': 'video_generated', 'duration_seconds': 5.0,
                'start_seconds': 0.0, 'end_seconds': 5.0,
                'workflow': 'workflows/S01.json', 'prompt': '人物站在原地挥手',
                'generation': {'aspect_ratio': '16:9', 'seed': 42},
                'video': {'path': str(current), 'rework_candidate': {'path': str(rework)}},
                'generation_history': [
                    {'task_id': 'task-history', 'video_path': str(history),
                     'workflow': 'workflows/history.json'},
                ],
            },
            {
                'id': 'S02', 'status': 'planned', 'duration_seconds': 3.0,
                'start_seconds': 5.0, 'end_seconds': 8.0,
            },
            {
                'id': 'S03', 'status': 'video_generated', 'duration_seconds': 4.0,
                'start_seconds': 8.0, 'end_seconds': 12.0,
                'workflow': 'workflows/S03.json',
                'generation': {'seed': 7},
                'video': {'path': str(missing)},
            },
            {
                'id': 'S04', 'status': 'video_generated', 'duration_seconds': 4.0,
                'start_seconds': 12.0, 'end_seconds': 16.0,
                'generation_history': [{'task_id': 'task-tied', 'video_path': str(tied)}],
                'video': {'path': str(tied)},
            },
        ],
        'assembly': None,
    }
    manifest = {'id': PROJECT_ID, 'title': '测试作品',
                'assembly_asset_id': 'MASTER', 'production_preset': None}
    records = []

    def require_manifest(project_id):
        if project_id != PROJECT_ID:
            raise HTTPException(404, '作品不存在')
        return manifest

    def load_plan(project_id):
        if project_id != PROJECT_ID:
            raise HTTPException(404, '作品不存在')
        return json.loads(json.dumps(plan)), workspace / 'plans' / 'plan.json'

    def resolve_registered_path(value):
        return Path(str(value)).resolve()

    def append_project_record(record_type, data, *, asset_id='', source='director-ui',
                              project_id=None, expected_revision=None):
        record = {
            'id': len(records) + 1,
            'record_type': record_type,
            'asset_id': asset_id,
            'data': data,
            'source': source,
            'project_id': project_id,
            'revision': len(records) + 1,
            'created_at_iso': '2026-10-20T00:00:00Z',
        }
        records.append(record)
        return record

    monkeypatch.setattr(backend, 'require_project_manifest', require_manifest)
    monkeypatch.setattr(backend, 'load_project_plan', load_plan)
    monkeypatch.setattr(backend, 'resolve_registered_path', resolve_registered_path)
    monkeypatch.setattr(backend, 'append_project_record', append_project_record)

    application = FastAPI()
    application.include_router(ROUTER)
    client = TestClient(application)
    return client, {
        'plan': plan,
        'records': records,
        'paths': {'current': str(current), 'rework': str(rework), 'history': str(history),
                  'tied': str(tied), 'missing': str(missing), 'foreign': str(foreign)},
    }


def capability(client, segment_id='S01'):
    return client.get(f'/api/projects/{PROJECT_ID}/segments/{segment_id}/refinement-capability')


def submit(client, segment_id='S01', **body):
    return client.post(f'/api/projects/{PROJECT_ID}/segments/{segment_id}/refinements', json=body)


def method_map(payload):
    return {item['method']: item for item in payload['methods']}


def error_detail(response):
    assert response.status_code == 422, response.text
    detail = response.json()['detail']
    assert isinstance(detail, dict), response.text
    return detail


def test_capability_lists_facts_required_inputs_and_structured_blockers(refinement_env):
    client, env = refinement_env
    response = capability(client)
    assert response.status_code == 200, response.text
    payload = response.json()
    methods = method_map(payload)
    assert set(methods) == set(refinement.METHODS)
    assert payload['available_methods'] == [
        refinement.METHOD_REGENERATE_WHOLE_SHOT,
        refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
    ]

    whole = methods[refinement.METHOD_REGENERATE_WHOLE_SHOT]
    assert whole['available'] is True and whole['blockers'] == []
    assert whole['required_inputs'] == ['base_candidate_ref', 'instruction']

    locked = methods[refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS]
    assert locked['available'] is True
    assert locked['required_inputs'] == ['base_candidate_ref', 'instruction', 'preserve']
    assert {'generation', 'prompt', 'workflow'} <= set(locked['preserve_fields'])

    interval = methods[refinement.METHOD_INTERVAL_EDIT]
    assert interval['available'] is False
    assert interval['required_inputs'] == ['base_candidate_ref', 'interval.start_seconds',
                                           'interval.end_seconds', 'instruction']
    assert 'interval_edit_unavailable' in {item['code'] for item in interval['blockers']}
    assert any(item.get('reason') == 'executor_not_installed' for item in interval['blockers'])

    assert payload['base_candidate']['available'] is True
    assert payload['base_candidate']['candidate_ref'] == env['paths']['current']
    assert {row['candidate_ref'] for row in payload['base_candidate']['candidates']} == {
        env['paths']['current'], env['paths']['rework'], env['paths']['history'],
    }
    assert payload['segment']['duration_seconds'] == 5.0
    assert payload['generation_submitted'] is False
    text = json.dumps(payload, ensure_ascii=False)
    assert 'recommended' not in text and 'default_method' not in text


def test_capability_reports_missing_candidate_and_recipe(refinement_env):
    client, _env = refinement_env
    planned = capability(client, 'S02')
    assert planned.status_code == 200
    methods = method_map(planned.json())
    whole_codes = {item['code'] for item in methods[refinement.METHOD_REGENERATE_WHOLE_SHOT]['blockers']}
    assert {'base_candidate_required', 'workflow_required'} <= whole_codes
    locked_codes = {item['code'] for item in methods[refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS]['blockers']}
    assert 'locked_params_unavailable' in locked_codes
    assert methods[refinement.METHOD_INTERVAL_EDIT]['available'] is False

    tied = capability(client, 'S04')
    assert tied.status_code == 200
    methods = method_map(tied.json())
    whole_codes = {item['code'] for item in methods[refinement.METHOD_REGENERATE_WHOLE_SHOT]['blockers']}
    assert 'workflow_required' in whole_codes
    assert 'base_candidate_required' not in whole_codes

    missing_segment = capability(client, 'missing')
    assert missing_segment.status_code == 404
    assert missing_segment.json()['detail']['code'] == 'segment_not_found'
    assert capability(client, 'S01').status_code == 200


def test_unknown_project_and_segment_are_not_found(refinement_env):
    client, env = refinement_env
    response = client.get(f'/api/projects/{OTHER_PROJECT}/segments/S01/refinement-capability')
    assert response.status_code == 404
    response = submit(client, segment_id='missing', method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['current'], instruction='重做')
    assert response.status_code == 404


def test_valid_whole_shot_request_is_recorded_without_submission(refinement_env):
    client, env = refinement_env
    response = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['current'],
                      instruction='让人物改成回头挥手')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['accepted'] is True
    assert payload['effect'] == 'request_recorded'
    assert payload['generation_submitted'] is False
    assert payload['refinement_id'] == 'S01:refinement:1'
    assert payload['record']['revision'] == 1
    assert len(env['records']) == 1
    record = env['records'][0]
    assert record['record_type'] == 'artifact:refinement'
    assert record['asset_id'] == 'S01'
    assert record['source'] == 'director-refinement'
    assert record['data']['status'] == 'accepted'
    assert record['data']['method'] == refinement.METHOD_REGENERATE_WHOLE_SHOT
    assert record['data']['instruction'] == '让人物改成回头挥手'
    assert record['data']['generation_submitted'] is False
    assert record['data']['execution'] == 'not_started'
    assert 'task_id' not in record['data']


def test_history_candidate_is_owned_and_foreign_candidate_is_rejected(refinement_env):
    client, env = refinement_env
    accepted = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['history'],
                      instruction='按历史版本重做动作')
    assert accepted.status_code == 200, accepted.text

    foreign = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                     base_candidate_ref=env['paths']['foreign'],
                     instruction='跨作品候选')
    detail = error_detail(foreign)
    assert detail['code'] == 'candidate_not_owned_by_shot'
    assert detail['segment_id'] == 'S01'
    assert len(env['records']) == 1

    missing = submit(client, segment_id='S03', method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                     base_candidate_ref=env['paths']['missing'],
                     instruction='缺失文件候选')
    assert error_detail(missing)['code'] == 'base_candidate_unavailable'
    assert len(env['records']) == 1


def test_empty_or_invalid_candidate_is_blocked(refinement_env):
    client, env = refinement_env
    empty = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                   base_candidate_ref='   ', instruction='重做')
    assert error_detail(empty)['code'] == 'base_candidate_required'

    invalid = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                     base_candidate_ref='not-a-registered-candidate', instruction='重做')
    assert error_detail(invalid)['code'] == 'candidate_not_owned_by_shot'
    assert env['records'] == []


def test_interval_validation_and_unavailable_interval_edit(refinement_env):
    client, env = refinement_env
    base = {'method': refinement.METHOD_INTERVAL_EDIT,
            'base_candidate_ref': env['paths']['current'], 'instruction': '只改这段动作'}

    unavailable = submit(client, interval={'start_seconds': 0.5, 'end_seconds': 2.0}, **base)
    detail = error_detail(unavailable)
    assert detail['code'] == 'interval_edit_unavailable'
    assert 'interval_edit_unavailable' in {item['code'] for item in detail['blockers']}
    assert env['records'] == []

    invalid = submit(client, interval={'start_seconds': 3.0, 'end_seconds': 2.0}, **base)
    assert error_detail(invalid)['code'] == 'invalid_interval'
    one_sided = submit(client, interval={'start_seconds': 1.0}, **base)
    assert error_detail(one_sided)['code'] == 'invalid_interval'
    negative = submit(client, interval={'start_seconds': -1.0, 'end_seconds': 2.0}, **base)
    assert error_detail(negative)['code'] == 'invalid_interval'

    out_of_bounds = submit(client, interval={'start_seconds': 3.0, 'end_seconds': 5.5}, **base)
    assert error_detail(out_of_bounds)['code'] == 'interval_out_of_bounds'
    missing = submit(client, **base)
    assert error_detail(missing)['code'] == 'interval_required'
    assert env['records'] == []


def test_interval_is_rejected_for_whole_shot_methods(refinement_env):
    client, env = refinement_env
    response = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['current'],
                      interval={'start_seconds': 0.0, 'end_seconds': 1.0},
                      instruction='重做')
    assert error_detail(response)['code'] == 'interval_not_supported_for_method'
    response = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                      base_candidate_ref=env['paths']['current'],
                      interval={'start_seconds': 0.0, 'end_seconds': 1.0},
                      preserve=['prompt'], instruction='重做')
    assert error_detail(response)['code'] == 'interval_not_supported_for_method'
    assert env['records'] == []


def test_locked_params_require_known_and_present_fields(refinement_env):
    client, env = refinement_env
    accepted = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                      base_candidate_ref=env['paths']['current'],
                      preserve=['generation', 'prompt', 'aspect_ratio'],
                      instruction='保持构图和种子，只改动作')
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['preserve'] == ['generation', 'prompt', 'aspect_ratio']
    assert accepted.json()['method'] == refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS
    assert env['records'][0]['data']['preserve'] == ['generation', 'prompt', 'aspect_ratio']

    empty = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                   base_candidate_ref=env['paths']['current'], preserve=[], instruction='重做')
    assert error_detail(empty)['code'] == 'preserve_required'
    unknown = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                     base_candidate_ref=env['paths']['current'], preserve=['mystery'],
                     instruction='重做')
    unknown_detail = error_detail(unknown)
    assert unknown_detail['code'] == 'preserve_field_unknown'
    assert unknown_detail['unknown_fields'] == ['mystery']
    unavailable = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                         base_candidate_ref=env['paths']['current'], preserve=['camera'],
                         instruction='重做')
    assert error_detail(unavailable)['code'] == 'preserve_field_unavailable'
    duplicate = submit(client, method=refinement.METHOD_REGENERATE_WITH_LOCKED_PARAMS,
                       base_candidate_ref=env['paths']['current'], preserve=['prompt', 'prompt'],
                       instruction='重做')
    assert error_detail(duplicate)['code'] == 'duplicate_preserve_field'
    assert len(env['records']) == 1


def test_preserve_is_rejected_for_whole_shot(refinement_env):
    client, env = refinement_env
    response = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['current'], preserve=['prompt'],
                      instruction='重做')
    assert error_detail(response)['code'] == 'preserve_not_supported_for_method'
    assert env['records'] == []


def test_unavailable_method_never_falls_back(refinement_env):
    client, env = refinement_env
    response = submit(client, segment_id='S04', method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['tied'], instruction='重做')
    detail = error_detail(response)
    assert detail['code'] == 'method_unavailable'
    assert 'workflow_required' in {item['code'] for item in detail['blockers']}
    assert env['records'] == []
    response = submit(client, segment_id='S02', method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                      base_candidate_ref=env['paths']['current'], instruction='重做')
    assert error_detail(response)['code'] == 'candidate_not_owned_by_shot'


def test_required_instruction_and_known_method(refinement_env):
    client, env = refinement_env
    empty_instruction = submit(client, method=refinement.METHOD_REGENERATE_WHOLE_SHOT,
                               base_candidate_ref=env['paths']['current'], instruction='  ')
    assert error_detail(empty_instruction)['code'] == 'instruction_required'
    unknown_method = submit(client, method='auto',
                            base_candidate_ref=env['paths']['current'], instruction='重做')
    detail = error_detail(unknown_method)
    assert detail['code'] == 'method_required'
    assert set(detail['allowed_methods']) == set(refinement.METHODS)
    assert env['records'] == []
