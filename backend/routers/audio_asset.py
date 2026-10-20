"""HTTP routes for extracting an adopted video's audio track into a project asset.

The routes are intentionally thin: :mod:`backend.audio_asset` owns adopted-source
validation, bounded FFmpeg extraction, idempotency and manifest registration.
Project ownership is not reimplemented here; the existing private-mode middleware
already scopes ``/api/projects/{project_id}/...`` requests to the current account.

Lead integration (same pattern as the other new domain routers, before app.py's
final ``/`` static mount, and re-export the module-level names)::

    from .routers.audio_asset import ROUTER as _audio_asset_router
    app.include_router(_audio_asset_router)
    from .routers.audio_asset import extract_project_audio, list_project_audio_assets
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import audio_asset


ROUTER = APIRouter()


@ROUTER.post('/api/projects/{project_id}/audio/extract')
def extract_project_audio(project_id: str, request: audio_asset.AudioExtractRequest) -> dict:
    """Extract one adopted video's first audio stream into a new WAV asset."""
    try:
        return audio_asset.extract_audio_asset(project_id, request)
    except audio_asset.AudioAssetError as exc:
        raise audio_asset.http_exception(exc) from exc


@ROUTER.get('/api/projects/{project_id}/audio/assets')
def list_project_audio_assets(project_id: str) -> dict:
    """List this project's registered audio assets without choosing or adopting."""
    try:
        return audio_asset.list_audio_assets(project_id)
    except audio_asset.AudioAssetError as exc:
        raise audio_asset.http_exception(exc) from exc
