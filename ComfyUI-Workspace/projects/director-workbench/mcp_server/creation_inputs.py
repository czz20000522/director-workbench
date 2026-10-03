"""Shared declarative input schema; no backend, routing, or task implementation."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['auto', 'advanced'] = 'auto'
    aspect_ratio: Literal['16:9', '1:1', '9:16'] = '16:9'
    sound_mode: Literal['performance_reference', 'locked_dialogue'] = 'performance_reference'
    seed: StrictInt = Field(default=42, ge=0, le=2147483647)
