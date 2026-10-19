"""Public MCP input models shared by the domain tool registrars."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .creation_inputs import GenerationSettings


class ShotChanges(BaseModel):
    generation: GenerationSettings | None = None
    workflow: str | None = Field(default=None, max_length=500)
    preset_id: str | None = Field(default=None, max_length=120)
    reference_asset_id: str | None = Field(default=None, max_length=160)
    model_config = ConfigDict(extra='forbid')
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, strict=True, ge=0)
    prompt: str | None = Field(default=None, max_length=100000)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    location: str | None = Field(default=None, max_length=160)
    shot_size: str | None = Field(default=None, max_length=80)
    camera: str | None = Field(default=None, max_length=160)
    wardrobe: str | None = Field(default=None, max_length=160)
    performance: str | None = Field(default=None, max_length=1000)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)
    dependencies: list[str] | None = Field(default=None, max_length=64)


class ShotMaterials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)


class ScriptScene(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    text: str | None = Field(default=None, max_length=500000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)
