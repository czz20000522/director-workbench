"""Ownerless ``needs_reconcile`` recovery: read-only plan and explicit dispositions.

This is a new domain module; the Lead includes ``ROUTER`` in ``backend/app.py`` at
the existing registration point and re-exports its route functions and request
models.  In private mode the Lead also adds the two ``/api/admin`` paths to
``private_mode``'s authorization surface.  Like the other split routers this
module reads runtime objects through ``backend.<name>`` at call time, so app.py
monkeypatching semantics stay unchanged and import order does not create a
cycle.

Business rules, permission checks and the DB/registry evidence live in
``backend.queue_recovery``; the routes stay thin so the same contract can be
tested without the full app.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .. import app as backend
from .. import queue_recovery
from ..queue_recovery import OwnerlessResolutionRequest, RepairOwnerRequest


ROUTER = APIRouter()


@ROUTER.get('/api/admin/tasks/recovery-plan')
def recovery_plan(limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    """Read-only: evidence, inferred owner and explicit actions; never writes."""
    return queue_recovery.admin_plan(backend, limit=limit)


@ROUTER.post('/api/admin/tasks/{task_id}/repair-owner')
def repair_owner(task_id: str, request: RepairOwnerRequest) -> dict[str, Any]:
    """Admin/local: repair the unique inferred owner, or mark needs_operator."""
    return queue_recovery.admin_repair(backend, task_id, request)


@ROUTER.post('/api/projects/{project_id}/tasks/{task_id}/resolve-ownerless')
def resolve_ownerless(project_id: str, task_id: str,
                      request: OwnerlessResolutionRequest) -> dict[str, Any]:
    """Project owner confirms the frozen row was never submitted; terminal, not success."""
    return queue_recovery.user_resolve(backend, project_id, task_id, request)
