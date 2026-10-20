"""Ownerless ``needs_reconcile`` recovery: a read-only plan plus explicit dispositions.

The stuck rows handled here are deliberately narrow: a task is a candidate only
when it is still ``needs_reconcile``, was never frozen to an owner, has no
``prompt_id`` and no ``submission_receipt``, and would occupy the single GPU
execution slot.  Because no submission identity exists, the frozen row itself is
the evidence that nothing was acknowledged by the executor.

This module never retries, never marks a task ``succeeded`` and never touches a
submitted execution.  It can do three explicit things:

* report the evidence and the uniquely inferable owner without writing;
* repair ``payload.owner_user`` for an administrator/local operator;
* mark an un-inferable row ``needs_operator`` and move it to a terminal status so
  it stops occupying the GPU slot, while keeping the unknown outcome explicit;
* let the project owner confirm that it was never submitted.

Existing task-control remediation remains the route for submitted receipts.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

class RepairOwnerRequest(BaseModel):
    confirm: bool = False
    note: str = Field(default='', max_length=2000)


class OwnerlessResolutionRequest(BaseModel):
    confirm_not_submitted: bool = False
    note: str = Field(default='', max_length=2000)


RECONCILE_STATUS = 'needs_reconcile'
RECOVERY_KEY = 'owner_recovery'
OWNER_REPAIRED = 'owner_repaired'
NEEDS_OPERATOR = 'needs_operator'
RESOLVED_NOT_SUBMITTED = 'resolved_not_submitted'
TERMINAL_STATUSES = frozenset({'succeeded', 'failed', 'stopped'})
DEFAULT_CPU_KINDS = frozenset({'audio_edit', 'audio_master', 'media_operation'})
MAX_HISTORY = 20


class QueueRecoveryError(Exception):
    """Structured refusal that a route maps to an HTTP error."""

    def __init__(self, status_code: int, code: str, message: str,
                 *, blockers: list[dict[str, Any]] | None = None):
        self.status_code = status_code
        self.detail = {'code': code, 'message': message, 'blockers': blockers or []}
        super().__init__(message)


# -- authorization helpers --------------------------------------------------------

def actor_for(principal: Any) -> dict[str, Any]:
    if principal is None:
        return {'kind': 'local'}
    return {'kind': 'admin' if principal.administrator else 'user',
            'username': principal.identity.username}


def require_recovery_operator(backend) -> Any:
    """Administrator or local (no private-mode principal) only; never a plain user."""
    principal = backend.current_principal.get()
    if principal is not None and not principal.administrator:
        raise HTTPException(403, {
            'code': 'admin_required',
            'message': '仅管理员或本机操作者可以查看并处置 ownerless needs_reconcile 任务。',
            'blockers': [{'code': 'admin_required', 'message': '需要管理员账号或本机操作入口。'}],
        })
    return principal


def require_project_principal(backend, project_id: str) -> Any:
    """Project authorization used by the owner confirmation route."""
    principal = backend.current_principal.get()
    if getattr(backend, 'PRIVATE_WORKSPACES', None) is None:
        return principal
    if principal is None:
        raise HTTPException(401, '请登录工作台')
    backend.PRIVATE_WORKSPACES.require(project_id, principal)
    return principal


# -- route-facing entry points ----------------------------------------------------

def admin_plan(backend, *, limit: int = 200) -> dict[str, Any]:
    require_recovery_operator(backend)
    return build_plan(backend, limit=limit)


def admin_repair(backend, task_id: str, request: Any) -> dict[str, Any]:
    principal = require_recovery_operator(backend)
    if not request.confirm:
        raise HTTPException(409, {
            'code': 'confirmation_required',
            'message': '需要显式确认：修复 owner，或无法推断时标记 needs_operator 并释放执行名额。',
            'blockers': [{'code': 'confirmation_required', 'message': '请传 confirm=true。'}],
        })
    try:
        return repair_owner(backend, task_id, actor=actor_for(principal), note=request.note)
    except QueueRecoveryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def user_resolve(backend, project_id: str, task_id: str, request: Any) -> dict[str, Any]:
    principal = require_project_principal(backend, project_id)
    try:
        return resolve_ownerless(backend, project_id, task_id, actor=actor_for(principal),
                                 note=request.note,
                                 confirm_not_submitted=request.confirm_not_submitted)
    except QueueRecoveryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


# -- evidence helpers -------------------------------------------------------------

def _payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get('payload')
    return payload if isinstance(payload, dict) else {}


def _recovery(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get(RECOVERY_KEY)
    return value if isinstance(value, dict) else {}


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cpu_kinds(backend) -> frozenset[str]:
    module = getattr(backend, 'task_scheduler', None)
    return frozenset(getattr(module, 'CPU_KINDS', DEFAULT_CPU_KINDS) or DEFAULT_CPU_KINDS)


def resource_of(backend, task: dict[str, Any]) -> str:
    return 'cpu' if _payload(task).get('kind') in _cpu_kinds(backend) else 'gpu'


def is_submitted(task: dict[str, Any]) -> bool:
    """Any acknowledgement identity means the row must go through receipt reconciliation."""
    payload = _payload(task)
    return bool(task.get('prompt_id')) or bool(payload.get('prompt_id')) or bool(payload.get('submission_receipt'))


def occupies_execution_slot(backend, task: dict[str, Any]) -> bool:
    return task.get('status') == RECONCILE_STATUS and resource_of(backend, task) == 'gpu'


def _project_ids(task: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (task.get('project_id'), _payload(task).get('project_id')):
        if isinstance(value, str) and value.strip() and value not in values:
            values.append(value)
    return values


def _registry(backend) -> tuple[dict[str, Any] | None, str | None]:
    workspaces = getattr(backend, 'PRIVATE_WORKSPACES', None)
    if workspaces is None:
        return None, None
    try:
        value = workspaces.read()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, str(exc) or 'ownership_registry_unreadable'
    return (value if isinstance(value, dict) else None), None


def _audit(action: str, actor: dict[str, Any], evidence: dict[str, Any], note: str) -> dict[str, Any]:
    return {'schema_version': 1, 'state': action, 'action': action, 'recorded_at': time.time(),
            'actor': dict(actor or {'kind': 'local'}), 'note': note.strip(),
            'evidence': evidence, 'source': 'queue_recovery'}


def _with_history(prior: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    history = [dict(item) for item in (prior.get('history') or []) if isinstance(item, dict)]
    history.append(entry)
    return {**entry, 'history': history[-MAX_HISTORY:]}


def _last_audit(payload: dict[str, Any]) -> dict[str, Any] | None:
    history = _recovery(payload).get('history')
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        return history[-1]
    return None


def _task_evidence(task: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(task)
    return {
        'status_before': task.get('status'),
        'project_ids': _project_ids(task),
        'asset_id': task.get('asset_id'),
        'prompt_id': task.get('prompt_id') or payload.get('prompt_id'),
        'submission_receipt': payload.get('submission_receipt'),
        'owner_user_before': payload.get('owner_user'),
        'resource': None,  # filled by callers that have a backend reference
    }


# -- owner inference --------------------------------------------------------------

def infer_owner(backend, task: dict[str, Any], *, registry: dict[str, Any] | None = None,
                registry_error: str | None = None) -> dict[str, Any]:
    """Infer one owner from the project registry; never guesses and never falls back."""
    if registry is None and registry_error is None:
        registry, registry_error = _registry(backend)
    project_ids = _project_ids(task)
    result: dict[str, Any] = {'project_id': project_ids[0] if len(project_ids) == 1 else None,
                              'project_ids': project_ids, 'owner': None, 'sources': [],
                              'reason': None, 'state': NEEDS_OPERATOR}
    if not project_ids:
        return {**result, 'reason': 'project_id_missing'}
    if len(project_ids) > 1:
        return {**result, 'reason': 'project_identity_conflict'}
    project_id = project_ids[0]
    if registry_error is not None:
        return {**result, 'reason': 'ownership_registry_unavailable'}
    if registry is None:
        return {**result, 'reason': 'ownership_registry_not_installed'}
    if project_id in (registry.get('retired') or {}):
        return {**result, 'reason': 'project_retired'}
    owner = (registry.get('projects') or {}).get(project_id)
    if not owner:
        return {**result, 'reason': 'owner_not_registered'}
    prior = _recovery(_payload(task))
    previous_owner = prior.get('owner_user') or _payload(task).get('owner_user')
    if not _is_empty(previous_owner) and previous_owner != owner:
        return {**result, 'reason': 'existing_owner_mismatch',
                'sources': [{'source': 'ownership_registry', 'project_id': project_id, 'owner': owner},
                            {'source': 'previous_recovery', 'project_id': project_id, 'owner': previous_owner}]}
    state = OWNER_REPAIRED if prior.get('state') == OWNER_REPAIRED and not _is_empty(previous_owner) else 'owner_repairable'
    return {**result, 'owner': owner, 'state': state, 'reason': None,
            'sources': [{'source': 'ownership_registry', 'project_id': project_id, 'owner': owner}]}


# -- read-only plan ---------------------------------------------------------------

def _exclusion_reasons(backend, task: dict[str, Any]) -> list[str]:
    payload = _payload(task)
    reasons: list[str] = []
    prior = _recovery(payload)
    if task.get('status') != RECONCILE_STATUS:
        reasons.append('status_not_needs_reconcile')
    if not _is_empty(payload.get('owner_user')):
        reasons.append('owner_already_frozen')
    if is_submitted(task):
        reasons.append('submitted_requires_receipt_reconciliation')
    if resource_of(backend, task) != 'gpu':
        reasons.append('not_gpu_resource')
    if prior.get('state') == RESOLVED_NOT_SUBMITTED:
        reasons.append('already_resolved_not_submitted')
    elif prior.get('state') == NEEDS_OPERATOR and task.get('status') in TERMINAL_STATUSES:
        reasons.append('already_needs_operator')
    return reasons


def _actions(task: dict[str, Any], state: str) -> list[dict[str, Any]]:
    task_id = task.get('id')
    project_id = task.get('project_id')
    actions: list[dict[str, Any]] = []
    if state in {'owner_repairable', 'owner_conflict', NEEDS_OPERATOR}:
        label = '修复任务 owner' if state == 'owner_repairable' else '标记 needs_operator 并释放执行名额'
        actions.append({'id': 'repair_owner', 'label': label,
                        'method': 'POST', 'path': f'/api/admin/tasks/{task_id}/repair-owner',
                        'body': {'confirm': True, 'note': '核对项目注册表后处置'}})
    if state in {'owner_repairable', OWNER_REPAIRED} and project_id:
        actions.append({'id': 'resolve_ownerless', 'label': '确认任务未提交并释放执行名额',
                        'method': 'POST',
                        'path': f'/api/projects/{project_id}/tasks/{task_id}/resolve-ownerless',
                        'body': {'confirm_not_submitted': True, 'note': '核对冻结 payload 无提交回执'}})
    return actions


CONFLICT_REASONS = frozenset({'project_identity_conflict', 'existing_owner_mismatch'})


def classify(backend, task: dict[str, Any], *, registry: dict[str, Any] | None = None,
             registry_error: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(actionable_entry, excluded_entry)``; at most one is non-None."""
    payload = _payload(task)
    prior = _recovery(payload)
    reasons = _exclusion_reasons(backend, task)
    inference = infer_owner(backend, task, registry=registry, registry_error=registry_error)
    base = {
        'task_id': task.get('id'),
        'project_id': task.get('project_id'),
        'asset_id': task.get('asset_id'),
        'status': task.get('status'),
        'created_at': task.get('created_at'),
        'updated_at': task.get('updated_at'),
        'resource': resource_of(backend, task),
        'occupies_execution_slot': occupies_execution_slot(backend, task),
        'owner_user': payload.get('owner_user'),
        'task_error': task.get('error'),
        'audit': _last_audit(payload),
    }
    if prior.get('state') == OWNER_REPAIRED and not _is_empty(payload.get('owner_user')):
        remaining = [reason for reason in reasons if reason != 'owner_already_frozen']
        if not remaining:
            entry = {**base, 'recovery_state': OWNER_REPAIRED,
                     'inferred_owner': payload.get('owner_user'), 'owner_sources': inference.get('sources') or [],
                     'blockers': [], 'actions': _actions(task, OWNER_REPAIRED)}
            return entry, None
    if reasons:
        return None, {**base, 'recovery_state': 'excluded', 'reasons': reasons,
                      'blockers': [], 'actions': []}
    if inference.get('state') == NEEDS_OPERATOR:
        conflict = inference.get('reason') in CONFLICT_REASONS
        state = 'owner_conflict' if conflict else NEEDS_OPERATOR
        blocker = {'code': inference.get('reason') or 'owner_unknown',
                   'message': ('项目归属信息互相冲突，不能猜测 owner；需管理员或本机操作者显式处置。' if conflict
                               else '无法从项目注册表唯一推断任务 owner；需管理员或本机操作者显式处置。')}
        entry = {**base, 'recovery_state': state, 'inferred_owner': None,
                 'owner_sources': inference.get('sources') or [], 'blockers': [blocker],
                 'actions': _actions(task, state)}
        return entry, None
    entry = {**base, 'recovery_state': inference.get('state'), 'inferred_owner': inference.get('owner'),
             'owner_sources': inference.get('sources') or [], 'blockers': [],
             'actions': _actions(task, inference.get('state') or 'owner_repairable')}
    return entry, None


def build_plan(backend, *, limit: int = 200) -> dict[str, Any]:
    """Read-only recovery plan; never writes the DB, registry or scheduler state."""
    registry, registry_error = _registry(backend)
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute(
            'SELECT * FROM tasks WHERE status = ? ORDER BY created_at,rowid',
            (RECONCILE_STATUS,)).fetchall()
    tasks = [backend.task_row(row) for row in rows]
    actionable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for task in tasks:
        entry, skip = classify(backend, task, registry=registry, registry_error=registry_error)
        if entry is not None:
            actionable.append(entry)
        elif skip is not None:
            excluded.append(skip)
    counts = {state: sum(item['recovery_state'] == state for item in actionable)
              for state in ('owner_repairable', OWNER_REPAIRED, NEEDS_OPERATOR, 'owner_conflict')}
    counts['excluded'] = len(excluded)
    blockers: list[dict[str, Any]] = []
    if registry_error is not None:
        blockers.append({'code': 'ownership_registry_unavailable',
                         'message': '作品归属注册表暂时不可读；不会猜测 owner。',
                         'reason': registry_error})
    return {
        'schema_version': 1,
        'read_only': True,
        'private_workspaces': getattr(backend, 'PRIVATE_WORKSPACES', None) is not None,
        'generated_at': time.time(),
        'counts': counts,
        'blockers': blockers,
        'tasks': actionable[:max(0, int(limit))],
        'excluded': excluded[:max(0, int(limit))],
        'note': ('只读分析：不会重试、不会标记成功、不会修改任务或作品归属。'
                 '修复 owner 或确认未提交都需要管理员/项目负责人的显式请求。'),
    }


def startup_sweep(backend, *, limit: int = 200) -> dict[str, Any]:
    """Release pre-submission ownerless rows so one bad row cannot lock the GPU.

    This is intentionally narrow: only rows selected by ``build_plan`` (ownerless,
    needs_reconcile, no prompt/receipt) are touched.  An inferable owner is
    repaired first, then the project owner's "never submitted" resolution is
    recorded by the system actor.  Conflicts become ``needs_operator``.  Unknown
    submitted work is never touched.
    """
    plan = build_plan(backend, limit=limit)
    handled: list[dict[str, Any]] = []
    for item in plan.get('tasks', []):
        task_id = item.get('task_id')
        if not task_id:
            continue
        state = item.get('recovery_state')
        actor = {'kind': 'system', 'username': 'startup-recovery'}
        note = '启动恢复：ownerless needs_reconcile 且无 prompt_id/提交回执，按冻结证据释放执行名额。'
        try:
            if state in {'owner_repairable', OWNER_REPAIRED}:
                repair_owner(backend, task_id, actor=actor, note=note)
                resolve_ownerless(backend, item.get('project_id', ''), task_id, actor=actor,
                                  note=note, confirm_not_submitted=True)
                handled.append({'task_id': task_id, 'outcome': 'resolved_not_submitted'})
            elif state in {'owner_conflict', NEEDS_OPERATOR}:
                repair_owner(backend, task_id, actor=actor, note=note)
                handled.append({'task_id': task_id, 'outcome': 'needs_operator'})
        except QueueRecoveryError as exc:
            handled.append({'task_id': task_id, 'outcome': 'blocked',
                            'code': exc.detail.get('code'), 'message': exc.detail.get('message')})
    return {'schema_version': 1, 'scanned': len(plan.get('tasks', [])), 'handled': handled}


# -- explicit dispositions --------------------------------------------------------

def _load_task(backend, task_id: str) -> dict[str, Any] | None:
    # Read the raw row: backend.get_task() verifies the frozen owner and would
    # reject exactly the ownerless rows this module exists to inspect.
    with backend.DB_LOCK, backend.db() as connection:
        row = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    return backend.task_row(row) if row else None


def _write_task(backend, task: dict[str, Any], *, status: str, payload: dict[str, Any], error: str | None,
                result: dict[str, Any] | None) -> bool:
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute(
            'UPDATE tasks SET status=?,payload=?,error=?,result=?,updated_at=?,stop_requested=0 '
            'WHERE id=? AND status=? AND updated_at=?',
            (status, json.dumps(payload, ensure_ascii=False), error,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             now, task.get('id'), task.get('status'), task.get('updated_at')),
        ).rowcount
        connection.commit()
    return bool(changed)


def _wake(backend) -> None:
    try:
        backend.TASK_SCHEDULER.wake()
    except Exception:  # pragma: no cover - the scheduler is optional in unit contexts
        return


def _result_payload(task: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    result = dict(task.get('result') or {})
    result['ownerless_recovery'] = audit
    return result


def _response(backend, task: dict[str, Any], *, action: str, idempotent: bool,
              blockers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = _payload(task)
    return {
        'task_id': task.get('id'),
        'project_id': task.get('project_id'),
        'asset_id': task.get('asset_id'),
        'status': task.get('status'),
        'owner_user': payload.get('owner_user'),
        'action': action,
        'idempotent': idempotent,
        'blockers': blockers or [],
        'audit': _last_audit(payload),
        'occupies_execution_slot': occupies_execution_slot(backend, task),
        'note': '处置记录已保存；未知执行结果不会被视为成功。',
    }


def repair_owner(backend, task_id: str, *, actor: dict[str, Any], note: str = '') -> dict[str, Any]:
    """Repair an inferable owner, or explicitly mark needs_operator without success."""
    task = _load_task(backend, task_id)
    if task is None:
        raise QueueRecoveryError(404, 'task_not_found', '任务不存在')
    payload = _payload(task)
    prior = _recovery(payload)
    if prior.get('state') == RESOLVED_NOT_SUBMITTED:
        raise QueueRecoveryError(409, 'already_resolved_not_submitted',
                                 '任务已由项目负责人确认未提交；该处置是终态，不能再次修复 owner。')
    if prior.get('state') == OWNER_REPAIRED and not _is_empty(payload.get('owner_user')):
        return _response(backend, task, action=OWNER_REPAIRED, idempotent=True)
    if prior.get('state') == NEEDS_OPERATOR and task.get('status') in TERMINAL_STATUSES:
        return _response(backend, task, action=NEEDS_OPERATOR, idempotent=True)
    if is_submitted(task):
        raise QueueRecoveryError(409, 'submitted_requires_receipt_reconciliation',
                                 '任务已有提交身份或回执；不能用 owner 修复释放执行名额，请核对原提交回执。',
                                 blockers=[{'code': 'submitted_requires_receipt_reconciliation',
                                            'message': '已提交任务只能通过现有回执核对/缺失执行确认流程处理。'}])
    reasons = _exclusion_reasons(backend, task)
    if reasons:
        raise QueueRecoveryError(409, 'task_not_ownerless_reconcilable',
                                 '任务不属于 ownerless needs_reconcile 可处置集合。',
                                 blockers=[{'code': reason, 'message': '该任务不满足 ownerless 且未提交的冻结证据。'}
                                           for reason in reasons])
    inference = infer_owner(backend, task)
    evidence = {**_task_evidence(task), 'resource': resource_of(backend, task),
                'inferred_owner': inference.get('owner'), 'owner_sources': inference.get('sources')}
    if inference.get('state') == 'owner_repairable' and inference.get('owner'):
        entry = _audit(OWNER_REPAIRED, actor, evidence, note)
        updated_payload = {**payload, 'owner_user': inference['owner'],
                           RECOVERY_KEY: _with_history(prior, entry)}
        changed = _write_task(backend, task, status=RECONCILE_STATUS, payload=updated_payload,
                              error=task.get('error'), result=task.get('result'))
        if not changed:
            raise QueueRecoveryError(409, 'task_changed', '任务在处置期间发生变化，请重新读取恢复计划。')
        return _response(backend, _load_task(backend, task_id) or task, action=OWNER_REPAIRED, idempotent=False)
    # No unique owner: mark the unknown explicitly and release the execution slot.
    entry = _audit(NEEDS_OPERATOR, actor, evidence, note)
    updated_payload = {**payload, RECOVERY_KEY: _with_history(prior, entry)}
    error = ('任务缺少可唯一推断的项目负责人且没有提交回执；已标记 needs_operator 并释放执行名额，'
             '未视为成功，需操作者核对原执行。')
    changed = _write_task(backend, task, status='stopped', payload=updated_payload, error=error,
                          result=_result_payload(task, entry))
    if not changed:
        raise QueueRecoveryError(409, 'task_changed', '任务在处置期间发生变化，请重新读取恢复计划。')
    _wake(backend)
    updated = _load_task(backend, task_id) or task
    return _response(backend, updated, action=NEEDS_OPERATOR, idempotent=False,
                     blockers=[{'code': inference.get('reason') or 'owner_unknown',
                                'message': '无法唯一推断 owner；已显式标记 needs_operator，未视为成功。'}])


def resolve_ownerless(backend, project_id: str, task_id: str, *, actor: dict[str, Any], note: str,
                      confirm_not_submitted: bool = False) -> dict[str, Any]:
    """Project-owner confirmation that the frozen row never reached the executor."""
    if not confirm_not_submitted:
        raise QueueRecoveryError(409, 'confirmation_required',
                                 '需要显式确认任务从未提交（confirm_not_submitted=true）。')
    note = (note or '').strip()
    if not note:
        raise QueueRecoveryError(422, 'note_required', '请填写核对冻结 payload 无提交回执的说明。')
    task = _load_task(backend, task_id)
    if task is None or task.get('project_id') != project_id:
        raise QueueRecoveryError(404, 'task_not_found', '该作品中不存在此任务')
    payload = _payload(task)
    prior = _recovery(payload)
    if prior.get('state') == RESOLVED_NOT_SUBMITTED:
        return _response(backend, task, action=RESOLVED_NOT_SUBMITTED, idempotent=True)
    if is_submitted(task):
        raise QueueRecoveryError(409, 'submitted_requires_receipt_reconciliation',
                                 '任务已有提交身份或回执，不能标记为未提交；请走原回执核对流程。',
                                 blockers=[{'code': 'submitted_requires_receipt_reconciliation',
                                            'message': '提交身份存在时只能核对回执或使用缺失执行确认。'}])
    blockers: list[dict[str, Any]] = []
    if resource_of(backend, task) != 'gpu':
        blockers.append({'code': 'not_gpu_resource', 'message': '该任务不占用 GPU 槽，请使用对应的执行器核对入口。'})
    if task.get('status') != RECONCILE_STATUS:
        blockers.append({'code': 'status_not_needs_reconcile',
                         'message': '任务已离开待核对状态，不能再按 ownerless 未提交处理。'})
    owner_user = payload.get('owner_user')
    actor_username = (actor or {}).get('username')
    # Startup recovery is a trusted system actor: it repaired the owner from the
    # registry in the same sweep and releases the frozen, never-submitted row.
    trusted_actor = (actor or {}).get('kind') in {'local', 'system'}
    if not _is_empty(owner_user) and not trusted_actor and owner_user != actor_username:
        blockers.append({'code': 'owner_mismatch', 'message': '任务冻结 owner 与当前账号不一致。'})
    if blockers:
        raise QueueRecoveryError(409, 'task_not_resolvable_ownerless',
                                 '任务不满足 ownerless 未提交处置条件。', blockers=blockers)
    evidence = {**_task_evidence(task), 'resource': resource_of(backend, task),
                'confirmation': 'not_submitted'}
    entry = _audit(RESOLVED_NOT_SUBMITTED, actor, evidence, note)
    entry['confirmed_not_submitted'] = True
    updated_payload = {**payload, RECOVERY_KEY: _with_history(prior, entry)}
    error = '用户确认任务从未提交到执行器；已释放执行名额，未视为成功。'
    changed = _write_task(backend, task, status='stopped', payload=updated_payload, error=error,
                          result=_result_payload(task, entry))
    if not changed:
        raise QueueRecoveryError(409, 'task_changed', '任务在处置期间发生变化，请重新读取。')
    _wake(backend)
    return _response(backend, _load_task(backend, task_id) or task, action=RESOLVED_NOT_SUBMITTED, idempotent=False)
