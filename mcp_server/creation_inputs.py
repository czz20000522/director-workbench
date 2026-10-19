"""Shared declarative input schema; no backend, routing, or task implementation."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    aspect_ratio: Literal['16:9', '1:1', '9:16'] = '16:9'
    output_resolution: Literal['native', '2k', '4k', '8k'] = 'native'
    sound_mode: Literal['performance_reference', 'locked_dialogue'] = 'performance_reference'
    seed: StrictInt = Field(default=42, ge=0, le=2147483647)
    speed_profile: Literal['standard', 'fast'] = 'standard'
    creative_profile: Literal['general', 'adult-realism', 'adult-anime'] = 'general'
