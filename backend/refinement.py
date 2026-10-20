"""显式分镜局部动作重做合同：能力事实、请求校验与无自动执行的请求记录。

本模块只定义纯合同逻辑，不读取数据库、不调度任务、不修改计划。HTTP 适配器
（``backend/routers/refinement.py``）负责从工作台读取作品/计划并把通过校验的请求
登记为 project artifact；第一版不提交生成。

平台边界：
- 只返回可用性事实与阻断项，不替调用方选择方法，也不包含 ``recommended``/默认方法；
- 不可用方法必须返回结构化 blocker，不能静默降级到其他方法；
- 候选必须属于该镜头当前登记候选或生成历史，且文件实际存在；
- 区间只对显式声明需要区间的 ``interval_edit`` 有效；其他方法收到区间必须拒绝而不是忽略。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, StrictFloat

METHOD_REGENERATE_WHOLE_SHOT = 'regenerate_whole_shot'
METHOD_REGENERATE_WITH_LOCKED_PARAMS = 'regenerate_with_locked_params'
METHOD_INTERVAL_EDIT = 'interval_edit'
METHODS = (METHOD_REGENERATE_WHOLE_SHOT, METHOD_REGENERATE_WITH_LOCKED_PARAMS, METHOD_INTERVAL_EDIT)
INTERVAL_METHOD = METHOD_INTERVAL_EDIT

# Fields the caller may ask the workbench to preserve. These name recipe/plan
# facts; they are not a recommendation to preserve any particular one.
PRESERVE_FIELDS = (
    'generation', 'prompt', 'duration_seconds', 'aspect_ratio', 'seed', 'speed_profile',
    'creative_profile', 'output_resolution', 'sound_mode', 'workflow', 'reference_asset_id',
    'location', 'shot_size', 'camera', 'wardrobe', 'performance', 'first_frame', 'last_frame',
    'audio_guide', 'delivery_master',
)
GENERATION_FIELDS = ('aspect_ratio', 'seed', 'speed_profile', 'creative_profile',
                     'output_resolution', 'sound_mode')
# A bare structural duration is not a locked creative parameter. Locked-parameter
# refinement needs at least one explicit recipe/performance/material fact.
LOCKABLE_FIELDS = tuple(field for field in PRESERVE_FIELDS if field != 'duration_seconds')
INSTRUCTION_MAX_LENGTH = 4000
MAX_CANDIDATE_REFS = 100

METHOD_REQUIRED_INPUTS = {
    METHOD_REGENERATE_WHOLE_SHOT: ('base_candidate_ref', 'instruction'),
    METHOD_REGENERATE_WITH_LOCKED_PARAMS: ('base_candidate_ref', 'instruction', 'preserve'),
    METHOD_INTERVAL_EDIT: ('base_candidate_ref', 'interval.start_seconds',
                           'interval.end_seconds', 'instruction'),
}


class RefinementError(Exception):
    """A structured refinement contract failure; routers translate it to HTTP."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 blockers: list[dict[str, Any]] | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.blockers = blockers or []
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {'code': self.code, 'message': self.message, 'retryable': False}
        if self.blockers:
            value['blockers'] = self.blockers
        value.update(self.details)
        return value


class RefinementInterval(BaseModel):
    """Explicit half-open temporal interval inside one shot; ``end > start``."""

    model_config = ConfigDict(extra='forbid')
    start_seconds: StrictFloat | None = None
    end_seconds: StrictFloat | None = None


class RefinementRequest(BaseModel):
    """Request body for `POST .../refinements`.

    Deliberately permissive at the type layer: empty candidate/instruction and
    method-specific combinations must produce structured contract errors instead
    of generic schema errors.
    """

    model_config = ConfigDict(extra='forbid')
    method: str = Field(default='')
    base_candidate_ref: str = Field(default='', max_length=1000)
    interval: RefinementInterval | None = None
    instruction: str = Field(default='', max_length=INSTRUCTION_MAX_LENGTH)
    preserve: list[str] = Field(default_factory=list, max_length=len(PRESERVE_FIELDS))


def blocker(code: str, message: str, *, kind: str = 'method', object_id: str | None = None,
            **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {'code': code, 'message': message}
    if object_id is not None:
        value['object'] = {'kind': kind, 'id': object_id}
    value.update(extra)
    return value


def find_segment(document: dict[str, Any], segment_id: str) -> dict[str, Any] | None:
    for segment in document.get('segments', []):
        if isinstance(segment, dict) and str(segment.get('id')) == segment_id:
            return segment
    return None


def candidate_refs(segment: dict[str, Any]) -> list[str]:
    """Registered candidate video refs for this shot, newest history included."""
    values: list[str] = []
    video = segment.get('video') if isinstance(segment.get('video'), dict) else {}
    for candidate in (video.get('path'), (video.get('rework_candidate') or {}).get('path'),
                      (video.get('finish_review') or {}).get('path')):
        if isinstance(candidate, str) and candidate.strip():
            values.append(candidate)
    history = segment.get('generation_history') if isinstance(segment.get('generation_history'), list) else []
    for item in reversed(history):
        if isinstance(item, dict) and isinstance(item.get('video_path'), str) and item['video_path'].strip():
            values.append(item['video_path'])
    return list(dict.fromkeys(values))


def current_candidate_ref(segment: dict[str, Any]) -> str | None:
    video = segment.get('video') if isinstance(segment.get('video'), dict) else {}
    value = video.get('path')
    return value if isinstance(value, str) and value.strip() else None


def explicit_recipe(segment: dict[str, Any]) -> str | None:
    """An explicit executed recipe for this shot; never inferred from project defaults."""
    for value in (segment.get('workflow'),):
        if isinstance(value, str) and value.strip():
            return value
    history = segment.get('generation_history') if isinstance(segment.get('generation_history'), list) else []
    for item in reversed(history):
        if isinstance(item, dict) and isinstance(item.get('workflow'), str) and item['workflow'].strip():
            return item['workflow']
    return None


def preserve_fields_present(segment: dict[str, Any]) -> list[str]:
    """Facts that currently exist on this shot and can therefore be preserved."""
    present: list[str] = []
    generation = segment.get('generation') if isinstance(segment.get('generation'), dict) else {}
    for field in PRESERVE_FIELDS:
        if field == 'generation':
            if generation:
                present.append(field)
        elif field in GENERATION_FIELDS:
            if field in generation:
                present.append(field)
        elif segment.get(field) not in (None, '', [], {}):
            present.append(field)
    return present


def lockable_fields_present(segment: dict[str, Any]) -> list[str]:
    return [field for field in preserve_fields_present(segment) if field in LOCKABLE_FIELDS]


def segment_duration(segment: dict[str, Any]) -> float | None:
    value = segment.get('duration_seconds')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _method_entry(method: str, blockers: list[dict[str, Any]], *, preserve_fields: list[str] | None = None) -> dict[str, Any]:
    return {
        'method': method,
        'available': not blockers,
        'required_inputs': list(METHOD_REQUIRED_INPUTS[method]),
        'blockers': blockers,
        'preserve_fields': list(preserve_fields or []),
    }


def capability(project_id: str, segment: dict[str, Any]) -> dict[str, Any]:
    """Return method availability facts and blockers; never chooses a method."""
    facts = capability_values(segment)
    candidates = candidate_refs(segment)
    current = current_candidate_ref(segment)
    present = preserve_fields_present(segment)
    duration = segment_duration(segment)
    start = segment.get('start_seconds')
    end = segment.get('end_seconds')
    candidate_rows = [
        {'candidate_ref': ref, 'is_current': ref == current}
        for ref in candidates[:MAX_CANDIDATE_REFS]
    ]
    return {
        'schema_version': 1,
        'project_id': project_id,
        'segment_id': str(segment.get('id')),
        'segment': {
            'id': str(segment.get('id')),
            'status': segment.get('status'),
            'duration_seconds': duration,
            'start_seconds': start if isinstance(start, (int, float)) and not isinstance(start, bool) else None,
            'end_seconds': end if isinstance(end, (int, float)) and not isinstance(end, bool) else None,
        },
        'base_candidate': {
            'available': bool(candidates),
            'candidate_ref': current,
            'candidates': candidate_rows,
            'count': len(candidates),
            'truncated': len(candidates) > MAX_CANDIDATE_REFS,
        },
        'methods': facts['methods'],
        'available_methods': [item['method'] for item in facts['methods'] if item['available']],
        'constraints': {
            'instruction_max_length': INSTRUCTION_MAX_LENGTH,
            'interval': {'start_min_seconds': 0, 'end_max_seconds': duration, 'end_exclusive': True},
            'preserve_fields_present': present,
            'preserve_fields_allowed': list(PRESERVE_FIELDS),
        },
        'blockers': [],
        'generation_submitted': False,
        'note': '能力查询只返回事实与阻断项；不提交生成，也不替调用方选择方法。',
    }


def _interval_value(interval: RefinementInterval | None) -> dict[str, float] | None:
    if interval is None:
        return None
    start, end = interval.start_seconds, interval.end_seconds
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise RefinementError(422, 'invalid_interval', '区间必须同时提供 start_seconds 和 end_seconds。')
    if not math.isfinite(start) or not math.isfinite(end):
        raise RefinementError(422, 'invalid_interval', '区间必须是有限秒数。')
    if start < 0:
        raise RefinementError(422, 'invalid_interval', '区间起点必须大于等于 0。')
    if end <= start:
        raise RefinementError(422, 'invalid_interval', '区间必须满足 start_seconds < end_seconds。')
    return {'start_seconds': round(float(start), 3), 'end_seconds': round(float(end), 3)}


def validate_interval_bounds(interval: dict[str, float], segment: dict[str, Any]) -> dict[str, float]:
    duration = segment_duration(segment)
    if duration is None:
        raise RefinementError(
            422, 'segment_duration_unknown',
            '无法确认该镜头时长，不能安全校验区间；请先核对镜头时长。',
            blockers=[blocker('segment_duration_unknown', '镜头缺少可校验的时长。',
                              kind='segment', object_id=str(segment.get('id')))],
        )
    if interval['end_seconds'] > duration:
        raise RefinementError(
            422, 'interval_out_of_bounds',
            f'区间结束时间超过镜头时长 {duration:g} 秒。',
            details={'duration_seconds': duration, 'interval': interval},
        )
    return interval


def validate_request(segment: dict[str, Any], request: RefinementRequest, *,
                     resolve_candidate: Callable[[str], Any]) -> dict[str, Any]:
    """Validate a request against one shot and return the normalized request facts.

    ``resolve_candidate`` resolves a candidate ref to a project-owned path (the
    router passes the existing registered-candidate resolver). Raises
    RefinementError with a structured code; never falls back to another method or
    ignores method-inapplicable fields.
    """
    method = request.method.strip()
    if method not in METHODS:
        raise RefinementError(
            422, 'method_required',
            '必须显式选择 regenerate_whole_shot、regenerate_with_locked_params 或 interval_edit。',
            details={'allowed_methods': list(METHODS)},
        )
    base_ref = request.base_candidate_ref.strip()
    if not base_ref:
        raise RefinementError(422, 'base_candidate_required', '必须提供 base_candidate_ref。')
    try:
        candidate = resolve_candidate(base_ref)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RefinementError(
            422, 'base_candidate_invalid', 'base_candidate_ref 无法解析为本作品可读候选。',
            details={'base_candidate_ref': base_ref},
        ) from exc
    if not candidate_matches_shot(candidate, segment, resolve_candidate):
        raise RefinementError(
            422, 'candidate_not_owned_by_shot',
            'base_candidate_ref 不是该镜头当前登记或历史生成候选；不能跨镜头/作品重做。',
            blockers=[blocker('candidate_not_owned_by_shot',
                              '候选不属于该镜头当前登记候选或生成历史。',
                              kind='candidate', object_id=base_ref)],
            details={'base_candidate_ref': base_ref, 'segment_id': str(segment.get('id'))},
        )
    try:
        candidate_exists = Path(str(candidate)).is_file()
    except OSError:
        candidate_exists = False
    if not candidate_exists:
        raise RefinementError(
            422, 'base_candidate_unavailable', 'base_candidate_ref 指向的候选文件不存在或不可读。',
            details={'base_candidate_ref': base_ref},
        )
    interval = _interval_value(request.interval)
    if method != INTERVAL_METHOD and interval is not None:
        raise RefinementError(
            422, 'interval_not_supported_for_method',
            f'{method} 不接受区间；只有 {INTERVAL_METHOD} 接受显式区间，不会静默忽略或改用其他方法。',
            details={'method': method},
        )
    if method == INTERVAL_METHOD and interval is None:
        raise RefinementError(422, 'interval_required', 'interval_edit 必须提供显式区间。')
    if interval is not None:
        interval = validate_interval_bounds(interval, segment)
    instruction = request.instruction.strip()
    if not instruction:
        raise RefinementError(422, 'instruction_required', '必须提供明确的重做 instruction。')
    preserve = list(dict.fromkeys(field.strip() for field in request.preserve))
    if any(not field for field in preserve):
        raise RefinementError(422, 'preserve_field_invalid', 'preserve 不能包含空字段。')
    if len(preserve) != len(request.preserve):
        raise RefinementError(422, 'duplicate_preserve_field', 'preserve 不得包含重复字段。')
    unknown = [field for field in preserve if field not in PRESERVE_FIELDS]
    if unknown:
        raise RefinementError(
            422, 'preserve_field_unknown', 'preserve 含未知字段；不会静默忽略。',
            details={'unknown_fields': unknown, 'allowed_fields': list(PRESERVE_FIELDS)},
        )
    present = preserve_fields_present(segment)
    unavailable = [field for field in preserve if field not in present]
    if unavailable:
        raise RefinementError(
            422, 'preserve_field_unavailable', 'preserve 含该镜头当前不存在的字段；无法锁定不存在的事实。',
            details={'unavailable_fields': unavailable, 'present_fields': present},
        )
    if method != METHOD_REGENERATE_WITH_LOCKED_PARAMS and preserve:
        raise RefinementError(
            422, 'preserve_not_supported_for_method',
            f'{method} 不接受 preserve；锁定参数必须使用 {METHOD_REGENERATE_WITH_LOCKED_PARAMS}。',
            details={'method': method},
        )
    if method == METHOD_REGENERATE_WITH_LOCKED_PARAMS and not preserve:
        raise RefinementError(422, 'preserve_required',
                              'regenerate_with_locked_params 必须明确列出要保留的参数。')
    facts = capability_values(segment)
    method_entry = next((item for item in facts['methods'] if item['method'] == method), None)
    if method_entry is None or not method_entry['available']:
        code = 'interval_edit_unavailable' if method == INTERVAL_METHOD else 'method_unavailable'
        raise RefinementError(
            422, code, f'{method} 当前不可用；不会静默降级到其他方法。',
            blockers=(method_entry or {}).get('blockers') or [],
            details={'method': method},
        )
    return {
        'method': method,
        'base_candidate_ref': base_ref,
        'interval': interval,
        'instruction': instruction,
        'preserve': preserve,
        'preserve_fields_present': present,
    }


def capability_values(segment: dict[str, Any]) -> dict[str, Any]:
    """Method facts without project/document context (private helper for validation)."""
    candidates = candidate_refs(segment)
    recipe = explicit_recipe(segment)
    present = preserve_fields_present(segment)
    lockable = lockable_fields_present(segment)
    missing_candidate = blocker('base_candidate_required',
                                '该镜头还没有已登记的生成候选；请先生成并确认一个候选镜头。',
                                object_id=str(segment.get('id')))
    missing_recipe = blocker('workflow_required',
                             '该镜头缺少显式制作方案/工作流；重做不会使用项目默认方案静默替代。',
                             object_id=str(segment.get('id')))
    no_preserve = blocker('locked_params_unavailable',
                          '该镜头没有可锁定的制作参数；无法使用锁定参数重做。',
                          object_id=str(segment.get('id')))
    interval_blocker = blocker(
        'interval_edit_unavailable',
        '第一版尚未接入局部区间重做执行器；请勿改用其他方法静默替代。',
        object_id=METHOD_INTERVAL_EDIT, reason='executor_not_installed',
        required_inputs=list(METHOD_REQUIRED_INPUTS[METHOD_INTERVAL_EDIT]),
    )
    whole_blockers = [*([missing_candidate] if not candidates else []), *([missing_recipe] if not recipe else [])]
    locked_blockers = [*whole_blockers, *([no_preserve] if not lockable else [])]
    interval_blockers = [*([missing_candidate] if not candidates else []), interval_blocker]
    return {
        'methods': [
            _method_entry(METHOD_REGENERATE_WHOLE_SHOT, whole_blockers),
            _method_entry(METHOD_REGENERATE_WITH_LOCKED_PARAMS, locked_blockers, preserve_fields=lockable),
            _method_entry(METHOD_INTERVAL_EDIT, interval_blockers, preserve_fields=present),
        ],
    }


def candidate_matches_shot(candidate: Any, segment: dict[str, Any], resolver) -> bool:
    """True when the resolved candidate path is one of this shot's registered candidates."""
    refs = candidate_refs(segment)
    if not refs:
        return False
    try:
        target = resolver(candidate)
    except (OSError, ValueError, RuntimeError):
        return False
    for ref in refs:
        try:
            if resolver(ref) == target:
                return True
        except (OSError, ValueError, RuntimeError):
            continue
    return False


def refinement_record(project_id: str, segment_id: str, facts: dict[str, Any], *,
                      source: str = 'director-refinement') -> dict[str, Any]:
    """Record data for the first version: accepted request, never an execution receipt."""
    return {
        'schema_version': 1,
        'project_id': project_id,
        'segment_id': segment_id,
        'method': facts['method'],
        'base_candidate_ref': facts['base_candidate_ref'],
        'interval': facts['interval'],
        'instruction': facts['instruction'],
        'preserve': facts['preserve'],
        'status': 'accepted',
        'generation_submitted': False,
        'execution': 'not_started',
        'source': source,
        'note': '只登记显式重做请求；未自动提交生成，也未自动采用任何候选。',
    }
