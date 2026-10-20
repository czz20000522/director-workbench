"""Explicit 2K/4K delivery upscale candidates: capability and task entry.

The router is a thin HTTP surface over :mod:`backend.delivery_upscale`. It never
chooses a source, target, method or audio policy, and it never adopts or
assembles the generated candidate. ``run_delivery_upscale_task`` is re-exported
for a future queued dispatch branch; the current route executes synchronously.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import app as backend
from .. import delivery_upscale
from ..delivery_upscale import DeliveryUpscaleRequest, run_delivery_upscale_task


ROUTER = APIRouter()


@ROUTER.get('/api/projects/{project_id}/upscale-capability')
def upscale_capability(project_id: str) -> dict[str, Any]:
    """Return available methods, blockers and limits; choose nothing."""
    backend.require_project_manifest(project_id)
    result = delivery_upscale.capability()
    return {**result, 'project_id': project_id}


@ROUTER.post('/api/projects/{project_id}/upscale-tasks')
def start_upscale_task(project_id: str, request: DeliveryUpscaleRequest) -> dict[str, Any]:
    """Generate one new delivery candidate from an explicitly adopted source.

    The request is synchronous in this version: the returned task is already
    succeeded or failed. The source stays untouched and the candidate is only
    registered as pending review.
    """
    try:
        return delivery_upscale.submit_request(project_id, request)
    except delivery_upscale.UpscaleError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
