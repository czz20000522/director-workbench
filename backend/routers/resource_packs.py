"""HTTP routes for series-level reusable resource packs.

The routes stay thin: ownership, series checks, path validation, media copying
and the published freeze live in ``backend.resource_packs``.  The module is
mounted by ``app.py`` next to the other routers.

Integration note: ``/api/series/...`` is not yet in the private-mode
authorization allowlist, and ``app.py`` does not include this router yet; both
are separate integration steps outside this module.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import resource_packs
from ..resource_packs import ResourcePackAssetCreate, ResourcePackCreate


ROUTER = APIRouter()


@ROUTER.get('/api/series/{series}/resource-packs')
def list_series_resource_packs(series: str) -> dict[str, Any]:
    return resource_packs.list_packs(series)


@ROUTER.post('/api/series/{series}/resource-packs', status_code=201)
def create_series_resource_pack(series: str, request: ResourcePackCreate) -> dict[str, Any]:
    return resource_packs.create_pack(
        series,
        pack_id=request.id,
        kind=request.kind,
        title=request.title,
    )


@ROUTER.get('/api/series/{series}/resource-packs/{pack_id}')
def read_series_resource_pack(series: str, pack_id: str) -> dict[str, Any]:
    return resource_packs.get_pack(series, pack_id)


@ROUTER.post('/api/series/{series}/resource-packs/{pack_id}/assets')
def add_series_resource_pack_asset(
    series: str,
    pack_id: str,
    request: ResourcePackAssetCreate,
) -> dict[str, Any]:
    return resource_packs.add_asset(
        series,
        pack_id,
        project_id=request.project_id,
        role=request.role,
        view=request.view,
        media_ref=request.media_ref,
        asset_id=request.asset_id,
    )


@ROUTER.post('/api/series/{series}/resource-packs/{pack_id}/publish')
def publish_series_resource_pack(series: str, pack_id: str) -> dict[str, Any]:
    return resource_packs.publish_pack(series, pack_id)


@ROUTER.get('/api/projects/{project_id}/resource-packs')
def list_project_resource_packs(project_id: str) -> dict[str, Any]:
    return resource_packs.available_for_project(project_id)
