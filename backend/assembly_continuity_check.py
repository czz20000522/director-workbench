"""Read-only cross-shot continuity check for long-form assembly.

The check consumes only explicit records that the workbench already stores:
the plan's segment order and timing, explicit adoption records, version-bound
join reviews, the selected sound policy and explicit reference bindings.  It
returns explainable checks and blockers; it never repairs anything, never
chooses or adopts a candidate and never reviews a join.

The module stays independent from :mod:`backend.app` so the same report can be
built by HTTP, MCP and tests from already collected facts.
"""
from __future__ import annotations

import math
from typing import Any, Callable

BLOCKER = 'blocker'
WARNING = 'warning'
PASS = 'pass'
NOT_APPLICABLE = 'not_applicable'
SEVERITIES = (BLOCKER, WARNING, PASS, NOT_APPLICABLE)

SOUND_POLICY_LABELS = {
    'segment_native': '保留各段原音',
    'complete_master': '统一完整音轨',
    'mute': '全片静音',
    'legacy_master': '作品登记的全片音轨',
}
REFERENCE_FIELDS = ('width', 'height', 'fps', 'duration_seconds')


def _row(code: str, category: str, severity: str, message: str, *, object_: dict[str, Any] | None = None,
         evidence: dict[str, Any] | None = None, action: dict[str, Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {'code': code, 'category': category, 'severity': severity, 'message': message}
    if object_ is not None:
        row['object'] = object_
    if evidence is not None:
        row['evidence'] = evidence
    if action is not None:
        row['action'] = action
    return row


def _segments(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in document.get('segments') or [] if isinstance(item, dict)]


def _segment_id(segment: dict[str, Any], index: int) -> str:
    value = segment.get('id')
    return str(value) if value else f'segment-{index + 1}'


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _plan_action(project_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    return {'id': 'inspect_plan', 'label': '查看分段计划', 'method': 'GET',
            'path': f'/api/projects/{project_id}/plan'}


def _segment_action(project_id: str, segment_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    return {'id': 'open_segment', 'label': '打开分镜并明确采用当前候选', 'method': 'GET',
            'path': f'/api/projects/{project_id}/plan/segments/{segment_id}'}


def _join_action(project_id: str, left_id: str, right_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    return {'id': 'review_join', 'label': '查看并审核接点', 'method': 'PUT',
            'path': f'/api/projects/{project_id}/assembly/joins/{left_id}/{right_id}'}


def _sound_action(project_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    return {'id': 'set_assembly_sound', 'label': '显式选择音轨方式', 'method': 'PUT',
            'path': f'/api/projects/{project_id}/assembly/sound'}


def _check_action(project_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    return {'id': 'recheck_continuity', 'label': '重新执行只读检查', 'method': 'GET',
            'path': f'/api/projects/{project_id}/assembly/continuity-check'}


def order_checks(document: dict[str, Any], *, project_id: str = '') -> list[dict[str, Any]]:
    """Report the explicit segment order and whether saved timing agrees with it."""
    segments = _segments(document)
    if not segments:
        return [_row('segments_required', 'order', BLOCKER, '装配需要至少一个显式分镜。',
                     action=_plan_action(project_id))]
    ids = [_segment_id(segment, index) for index, segment in enumerate(segments)]
    checks: list[dict[str, Any]] = []
    if any(not str(item).strip() for item in ids) or len(set(ids)) != len(ids):
        checks.append(_row('segment_order_invalid', 'order', BLOCKER,
                           '分镜 ID 必须非空且唯一；当前显式顺序不能用于装配。',
                           evidence={'segment_order': ids}, action=_plan_action(project_id)))
    else:
        checks.append(_row('segment_order', 'order', PASS,
                           f'按显式计划顺序装配 {len(ids)} 个分镜：{" → ".join(ids)}。',
                           evidence={'segment_order': ids}))
    cursor = 0.0
    measured = False
    drift: list[dict[str, Any]] = []
    unmetered: list[str] = []
    for index, segment in enumerate(segments):
        segment_id = _segment_id(segment, index)
        duration = _finite(segment.get('duration_seconds'))
        if duration is None or duration <= 0:
            unmetered.append(segment_id)
            continue
        measured = True
        expected_start = round(cursor, 3)
        expected_end = round(cursor + duration, 3)
        recorded_start = _finite(segment.get('start_seconds'))
        recorded_end = _finite(segment.get('end_seconds'))
        if recorded_start is not None and abs(recorded_start - expected_start) > 0.05:
            drift.append({'segment_id': segment_id, 'field': 'start_seconds',
                          'expected': expected_start, 'recorded': round(recorded_start, 3)})
        elif recorded_end is not None and abs(recorded_end - expected_end) > 0.05:
            drift.append({'segment_id': segment_id, 'field': 'end_seconds',
                          'expected': expected_end, 'recorded': round(recorded_end, 3)})
        cursor += duration
    if drift:
        checks.append(_row('segment_timing_mismatch', 'order', BLOCKER,
                           '显式分镜顺序与已保存的起止时间不一致；请先核对计划时长，检查不会自动改写时间轴。',
                           evidence={'differences': drift, 'expected_total_seconds': round(cursor, 3)},
                           action=_plan_action(project_id)))
    elif measured:
        checks.append(_row('segment_timing', 'order', PASS,
                           f'按显式顺序累计 {round(cursor, 3)} 秒，与已保存的分镜起止记录一致。',
                           evidence={'expected_total_seconds': round(cursor, 3)}))
    if unmetered:
        checks.append(_row('segment_duration_missing', 'order', WARNING,
                           '部分分镜缺少可用的正数时长，无法核对顺序取片；装配会拒绝这些分镜。',
                           evidence={'segment_ids': unmetered}, action=_plan_action(project_id)))
    return checks


def video_checks(video_blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the existing current-video blockers into explainable checks."""
    checks: list[dict[str, Any]] = []
    for blocker in video_blockers:
        code = str(blocker.get('code') or 'video_required')
        dependency = str(blocker.get('dependency') or '')
        message = str(blocker.get('message') or '分镜当前视频不可用。')
        checks.append(_row(code, 'media', BLOCKER, message,
                           object_=blocker.get('object') or {'kind': 'segment', 'id': dependency},
                           evidence={'segment_id': dependency} if dependency else None))
    return checks


def adoption_checks(adoption_blockers: list[dict[str, Any]], *, segment_count: int,
                    project_id: str = '') -> list[dict[str, Any]]:
    """Surface the unchanged explicit-adoption gate as continuity evidence."""
    checks: list[dict[str, Any]] = []
    for blocker in adoption_blockers:
        dependency = str(blocker.get('dependency') or '')
        checks.append(_row(str(blocker.get('code') or 'adoption_required'), 'adoption', BLOCKER,
                           str(blocker.get('message') or f'{dependency} 尚未有明确采用记录。'),
                           object_=blocker.get('object') or {'kind': 'segment', 'id': dependency},
                           evidence={'segment_id': dependency} if dependency else None,
                           action=_segment_action(project_id, dependency)))
    if segment_count and not adoption_blockers:
        checks.append(_row('adoption', 'adoption', PASS,
                           '所有分镜都有当前候选的明确采用记录（任务版本与文件身份已绑定）。',
                           evidence={'segment_count': segment_count}))
    return checks


def join_checks(joins: list[dict[str, Any]], *, segment_count: int, tracking: str = 'tracked',
                project_id: str = '') -> list[dict[str, Any]]:
    """Report version-bound join review state without changing its gate semantics."""
    checks: list[dict[str, Any]] = []
    if segment_count < 2:
        if segment_count:
            checks.append(_row('joins', 'join', NOT_APPLICABLE,
                               '只有一个分镜，不产生相邻接点审核项。'))
        return checks
    completed = 0
    for row in joins:
        key = str(row.get('key') or f"{row.get('left_id')}>{row.get('right_id')}")
        left_id = str(row.get('left_id') or '')
        right_id = str(row.get('right_id') or '')
        action = _join_action(project_id, left_id, right_id)
        if row.get('review_completed'):
            completed += 1
            continue
        status = str(row.get('status') or 'pending')
        if not row.get('review_ready'):
            code = 'join_video_required'
            message = f'{key} 接点两侧尚无当前视频，无法审核。'
        elif status == 'stale':
            code = 'join_review_stale'
            message = f'{key} 接点两侧视频或分镜顺序已变化，原审核结论过期；需重新查看并审核。'
        elif status in {'redo_left', 'redo_right'}:
            code = 'join_redo_required'
            message = f'{key} 接点仍标记为需重做（{status}）；完成后需重新审核。'
        else:
            code = 'join_review_required'
            message = f'{key} 接点尚未按当前视频版本审核通过。'
        if tracking == 'legacy_untracked' and code == 'join_review_required':
            checks.append(_row('join_review_untracked', 'join', WARNING,
                               f'{key} 尚无接点审核记录（旧版装配项目）；人工审核仍是装配权威。',
                               object_={'kind': 'join', 'id': key},
                               evidence={'join': key, 'join_tracking': 'legacy_untracked'},
                               action=action))
            continue
        checks.append(_row(code, 'join', BLOCKER, message,
                           object_={'kind': 'join', 'id': key},
                           evidence={'join': key, 'left_id': left_id, 'right_id': right_id,
                                     'status': status, 'join_tracking': tracking},
                           action=action))
    if joins and completed == len(joins):
        checks.append(_row('join_reviews', 'join', PASS,
                           f'{len(joins)} 个相邻接点均已按当前视频版本审核通过。',
                           evidence={'join_count': len(joins)}))
    return checks


def sound_checks(sound: dict[str, Any] | None, sound_blockers: list[dict[str, Any]], *,
                 project_id: str = '') -> list[dict[str, Any]]:
    """Report the explicit sound policy and its pre-assembly validation state."""
    checks: list[dict[str, Any]] = []
    for blocker in sound_blockers:
        checks.append(_row(str(blocker.get('code') or 'sound_policy_invalid'), 'sound', BLOCKER,
                           str(blocker.get('message') or '音轨方式无效。'),
                           object_={'kind': 'assembly', 'id': 'sound'},
                           evidence=blocker.get('evidence'),
                           action=blocker.get('action') or _sound_action(project_id)))
    if sound_blockers:
        return checks
    policy = str((sound or {}).get('policy') or '')
    label = SOUND_POLICY_LABELS.get(policy, policy or '未记录')
    evidence = {'policy': policy or None,
                'selected': bool((sound or {}).get('selected')),
                'selection_source': (sound or {}).get('selection_source')}
    if policy == 'complete_master' and (sound or {}).get('duration_seconds') is None:
        checks.append(_row('sound_duration_unverified', 'sound', WARNING,
                           '统一完整音轨的时长尚未验证（非 WAV 母版），成片后须核对尾部声音。',
                           evidence={'policy': policy}, action=_sound_action(project_id)))
    checks.append(_row('sound_policy', 'sound', PASS, f'已显式选择音轨方式：{label}。',
                       evidence=evidence))
    return checks


def reference_checks(document: dict[str, Any], *, project_id: str = '',
                     reference_resolver: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                     media_facts: Callable[[str], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Compare explicit reference facts with the current candidate; never a gate."""
    if reference_resolver is None:
        return []
    checks: list[dict[str, Any]] = []
    for index, segment in enumerate(_segments(document)):
        segment_id = _segment_id(segment, index)
        reference_id = segment.get('reference_asset_id')
        analysis_id = segment.get('source_analysis')
        if not reference_id and not analysis_id:
            continue
        binding = {'kind': 'asset' if reference_id else 'analysis', 'id': str(reference_id or analysis_id)}
        warning_base: dict[str, Any] = {'segment_id': segment_id, 'reference': binding}
        try:
            reference = reference_resolver(segment)
        except Exception:  # A missing reference must not break the read-only report.
            reference = None
        if not isinstance(reference, dict):
            checks.append(_row('reference_unavailable', 'reference', WARNING,
                               f'{segment_id} 已绑定参考，但当前无法读取参考素材；装配前请人工核对。',
                               object_={'kind': 'segment', 'id': segment_id},
                               evidence=warning_base, action=_segment_action(project_id, segment_id)))
            continue
        reference_facts = reference.get('facts') if isinstance(reference.get('facts'), dict) else {}
        current = {'known': False, 'reason': 'current_video_missing'}
        video_path = (segment.get('video') or {}).get('path')
        if media_facts is not None and video_path:
            try:
                current = media_facts(str(video_path))
            except Exception:
                current = {'known': False, 'reason': 'current_media_unavailable'}
        evidence = {'segment_id': segment_id, 'reference': binding,
                    'reference_facts': {field: reference_facts.get(field) for field in REFERENCE_FIELDS},
                    'current_facts': {field: current.get(field) for field in REFERENCE_FIELDS}}
        if reference_facts.get('known') and current.get('known'):
            differences = []
            for field in REFERENCE_FIELDS:
                left, right = reference_facts.get(field), current.get(field)
                left_value, right_value = _finite(left), _finite(right)
                if left_value is None or right_value is None:
                    continue
                if abs(left_value - right_value) > 0.001:
                    differences.append({'field': field, 'reference': left, 'current': right})
            if differences:
                checks.append(_row('reference_current_difference', 'reference', WARNING,
                                   f'{segment_id} 的参考素材与当前候选在 {len(differences)} 个可测维度上不同；'
                                   '这是事实差异，不是质量判断。',
                                   object_={'kind': 'segment', 'id': segment_id},
                                   evidence={**evidence, 'differences': differences},
                                   action=_segment_action(project_id, segment_id)))
            else:
                checks.append(_row('reference_current_match', 'reference', PASS,
                                   f'{segment_id} 的参考素材与当前候选在可测维度上一致。',
                                   object_={'kind': 'segment', 'id': segment_id}, evidence=evidence))
        else:
            checks.append(_row('reference_current_unmeasured', 'reference', WARNING,
                               f'{segment_id} 的参考或当前候选媒体事实不可测，参考/当前差异未得出；不会自动判定一致。',
                               object_={'kind': 'segment', 'id': segment_id},
                               evidence={**evidence, 'reference_reason': reference_facts.get('reason'),
                                         'current_reason': current.get('reason')},
                               action=_segment_action(project_id, segment_id)))
    return checks


def previous_output_checks(document: dict[str, Any]) -> list[dict[str, Any]]:
    assembly = document.get('assembly')
    if isinstance(assembly, dict) and assembly.get('status') == 'stale':
        return [_row('previous_assembly_stale', 'output', WARNING,
                     '制作计划已变化，上次成片被标记为过期；本次装配会生成新候选并保留旧文件。',
                     object_={'kind': 'assembly', 'id': 'MASTER'})]
    return []


def build_report(manifest: dict[str, Any] | None, document: dict[str, Any], *,
                 video_blockers: list[dict[str, Any]] | None = None,
                 adoption_blockers: list[dict[str, Any]] | None = None,
                 joins: list[dict[str, Any]] | None = None,
                 sound: dict[str, Any] | None = None,
                 sound_blockers: list[dict[str, Any]] | None = None,
                 join_tracking: str = 'tracked',
                 reference_resolver: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                 media_facts: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return one stable, JSON-serializable report; callers keep the plan read-only."""
    project_id = str((manifest or {}).get('id') or document.get('project_id') or '')
    segments = _segments(document)
    checks: list[dict[str, Any]] = []
    checks.extend(order_checks(document, project_id=project_id))
    checks.extend(video_checks(list(video_blockers or [])))
    checks.extend(adoption_checks(list(adoption_blockers or []), segment_count=len(segments),
                                  project_id=project_id))
    checks.extend(join_checks(list(joins or []), segment_count=len(segments),
                              tracking=join_tracking, project_id=project_id))
    checks.extend(sound_checks(sound, list(sound_blockers or []), project_id=project_id))
    checks.extend(reference_checks(document, project_id=project_id,
                                   reference_resolver=reference_resolver, media_facts=media_facts))
    checks.extend(previous_output_checks(document))
    blockers = [item for item in checks if item['severity'] == BLOCKER]
    warnings = [item for item in checks if item['severity'] == WARNING]
    reference_bindings = []
    for index, segment in enumerate(segments):
        reference_id = segment.get('reference_asset_id')
        analysis_id = segment.get('source_analysis')
        if reference_id or analysis_id:
            reference_bindings.append({'segment_id': _segment_id(segment, index),
                                       'kind': 'asset' if reference_id else 'analysis',
                                       'id': str(reference_id or analysis_id)})
    return {
        'schema_version': 1,
        'project_id': project_id,
        'revision': int(document.get('revision', 0) or 0),
        'read_only': True,
        'automatic_actions': False,
        'ready': bool(segments) and not blockers,
        'counts': {
            'blocker': len(blockers),
            'warning': len(warnings),
            'pass': sum(item['severity'] == PASS for item in checks),
            'not_applicable': sum(item['severity'] == NOT_APPLICABLE for item in checks),
        },
        'inputs': {
            'segment_order': [_segment_id(segment, index) for index, segment in enumerate(segments)],
            'join_review': join_tracking,
            'sound': {
                'selected': bool((sound or {}).get('selected')),
                'policy': (sound or {}).get('policy'),
                'selection_source': (sound or {}).get('selection_source'),
            },
            'reference_bindings': reference_bindings,
        },
        'checks': checks,
        'blockers': blockers,
        'warnings': warnings,
        'action': _check_action(project_id),
        'note': '只读检查：不自动修复、不自动选择候选、不自动采用或审核；阻断项需人工处理后重新检查。',
    }
