"""导演工作台请求/响应模型。

原先散落在 backend/app.py：主体在 1848-2098 行，另有若干处与各自的路由相邻。
app.py 只重新导出这些名字。pydantic/OpenAPI 的组件名取自类的 __name__，与定义
模块无关，所以 /openapi.json 的组件名（前端与测试依赖）保持不变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from . import shot_creation


class StartRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    asset_id: str = Field(min_length=1, max_length=160)
    prompt: str | None = None
    seed: int | None = None
    pipeline_stage_id: str | None = Field(default=None, max_length=120)
    pipeline_values: dict[str, Any] = Field(default_factory=dict)


class FinishRequest(BaseModel):
    expected_plan_revision: StrictInt = Field(ge=0)
    expected_review_revision: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    asset_id: str = Field(min_length=1, max_length=160)
    model_name: str = Field(default='RealESRGAN_x2plus.pth', min_length=1, max_length=200)
    color_reference: str | None = Field(default=None, max_length=500)
    color_method: Literal['uniform', 'per_frame'] = 'uniform'
    color_strength: float = Field(default=0.2, ge=0, le=1)


class BatchRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    asset_ids: list[str] = Field(min_length=1, max_length=128)
    prompt: str | None = None
    pipeline_stage_id: str | None = Field(default=None, max_length=120)
    pipeline_values_by_asset: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PipelineValidateRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    asset_id: str | None = Field(default=None, max_length=160)


class ProjectSelectRequest(BaseModel):
    project_id: str


class ProjectImportRequest(BaseModel):
    path: str
    project_id: str | None = None


class ProjectCreateRequest(BaseModel):
    # Creation mode is explicit and no longer has an implicit default.  `auto`
    # was removed by Issue #116; only the explicit advanced contract remains.
    creation_mode: Literal['advanced']
    series: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    project_id: str | None = None
    select: bool = True


class SeriesCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AssetLabelRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    expected_title: str


class DirectoryDeleteRequest(BaseModel):
    plan_id: str
    confirm: str


class SegmentDeleteRequest(BaseModel):
    expected_revision: int
    confirm: str


class PipelineStageCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(default="", max_length=500)
    backend: str = Field(default="混合流程", max_length=40)
    inputs: list[str] = Field(default_factory=list, max_length=32)
    outputs: list[str] = Field(default_factory=list, max_length=32)
    workflow: str = Field(default="", max_length=500)


ShotGenerationRequest = shot_creation.GenerationSettings


class PlanSegmentCreateRequest(BaseModel):
    generation: ShotGenerationRequest | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, ge=0)
    segment_id: str | None = None
    duration_seconds: float = Field(gt=0, le=600)
    location: str = Field(default="待定场景", max_length=160)
    shot_size: str = Field(default="中景", max_length=80)
    camera: str = Field(default="固定机位", max_length=160)
    wardrobe: str = Field(default="沿用角色设定", max_length=160)
    performance: str = Field(default="", max_length=1000)
    prompt: str = Field(default="", max_length=100000)
    workflow: str | None = Field(default=None, max_length=500)
    preset_id: str | None = Field(default=None, max_length=120)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)


class CreativeSettingsRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    values: dict[str, Any] = Field(default_factory=dict)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    source: str = Field(default="director-ui", max_length=80)


class ReviewRecordRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    expected_plan_revision: StrictInt | None = Field(default=None, ge=0)
    asset_id: str = Field(min_length=1, max_length=160)
    stage: Literal['sample', 'finish', 'final'] = 'sample'
    status: Literal['pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    note: str = Field(default="", max_length=4000)
    adopted_variant: str | None = Field(default=None, max_length=80)
    candidate_ref: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="director-ui", max_length=80)


class CheckpointRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    stage_id: str = Field(min_length=1, max_length=120)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    asset_id: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=4000)
    upstream_revisions: dict[str, int] = Field(default_factory=dict)
    source: str = Field(default="director-ui", max_length=80)


class ArtifactRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=240)
    content: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list, max_length=64)
    status: Literal['draft', 'pending_review', 'approved', 'changes_requested', 'stale'] = 'pending_review'
    asset_id: str = Field(default="", max_length=160)
    source: str = Field(default="director-ui", max_length=80)


class AdoptionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=160)
    stage: Literal['sample', 'finish', 'final']
    candidate_ref: str = Field(min_length=1, max_length=1000)
    expected_review_revision: StrictInt = Field(ge=0)
    expected_plan_revision: StrictInt = Field(ge=0)
    note: str = Field(min_length=1, max_length=4000)
    confirm_adoption: StrictBool = Field(json_schema_extra={'const': True})

    @field_validator('confirm_adoption')
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError('需要明确确认采用此候选')
        return value


class ReverseArtifactUpdateRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    summary: str = Field(default="", max_length=8000)
    user_status: str = Field(default="reviewed", max_length=40)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReverseConvertRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class ReverseClassificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    content_mode: Literal['narrative', 'music_performance', 'dance', 'showcase', 'mood', 'technical', 'other']
    reason: str = Field(min_length=1, max_length=2000)


class ReverseTimelineUpdateRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    user_note: str = Field(default="", max_length=4000)
    status: str = Field(default="reviewed", max_length=40)


class MaterialRegisterItem(BaseModel):
    source_id: str = Field(min_length=1, max_length=40)
    relative_path: str = Field(min_length=1, max_length=1000)


class MaterialRegisterRequest(BaseModel):
    items: list[MaterialRegisterItem] = Field(min_length=1, max_length=128)


class MaterialReuseRequest(BaseModel):
    source_project_id: str = Field(min_length=1, max_length=120)
    material_ids: list[str] = Field(min_length=1, max_length=128)


class PlanSegmentUpdateRequest(BaseModel):
    generation: ShotGenerationRequest | None = None
    reference_asset_id: str | None = Field(default=None, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)
    script_scene_id: str | None = Field(default=None, max_length=160)
    expected_script_revision: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    location: str | None = Field(default=None, max_length=160)
    shot_size: str | None = Field(default=None, max_length=80)
    camera: str | None = Field(default=None, max_length=160)
    wardrobe: str | None = Field(default=None, max_length=160)
    performance: str | None = Field(default=None, max_length=1000)
    prompt: str | None = Field(default=None, max_length=100000)
    workflow: str | None = Field(default=None, max_length=500)
    preset_id: str | None = Field(default=None, max_length=120)
    first_frame: str | None = Field(default=None, max_length=500)
    last_frame: str | None = Field(default=None, max_length=500)
    audio_guide: str | None = Field(default=None, max_length=500)
    delivery_master: str | None = Field(default=None, max_length=500)
    dependencies: list[str] | None = Field(default=None, max_length=64)


class SegmentVersionRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_revision: StrictInt = Field(ge=0)


class RecipeIdentityRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=160)
    expected_revision: StrictInt = Field(ge=0)
    dry_run: bool = True


class PlanSegmentOperationRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    operation: str = Field(max_length=40)
    target_id: str | None = Field(default=None, max_length=160)
    split_at_seconds: float | None = Field(default=None, gt=0, le=600)
    before_id: str | None = Field(default=None, max_length=160)


class GuideSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    status: Literal['active', 'skipped'] | None = None
    project_id: str | None = Field(default=None, max_length=160)
    route: Literal['idea', 'reference'] | None = None
    completed_steps: list[str] | None = Field(default=None, max_length=12)


class ScriptScene(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    text: str | None = Field(default=None, max_length=500000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)

    @field_validator('id', 'title')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('场次编号与标题不能为空')
        if value != value.strip():
            raise ValueError('场次编号与标题不能含首尾空白')
        return value


class ScriptUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    title: str = Field(default='', max_length=240)
    text: str = Field(max_length=2000000)
    target_duration_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    scenes: list[ScriptScene] = Field(default_factory=list, max_length=1000)


class KeyframeRequest(BaseModel):
    mode: Literal['reference', 'text'] = 'reference'
    creative_profile: Literal['general', 'adult-realism', 'adult-anime'] = 'general'
    prompt: str = Field(min_length=1, max_length=6000)
    reference_image: str | None = Field(default=None, min_length=1, max_length=1000)
    width: int = Field(default=768, ge=512, le=1536)
    height: int = Field(default=1344, ge=512, le=1536)
    output_resolution: Literal['native', '2k', '4k', '8k'] = 'native'
    seed: int = Field(default=340921, ge=0, le=2**64 - 1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=2000)
    mode: Literal['reference', 'design'] = 'reference'
    voice_reference: str | None = Field(default=None, min_length=1, max_length=1000)
    voice_description: str | None = Field(default=None, min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    gen_seconds: float = Field(default=4.5, gt=0, le=30, allow_inf_nan=False)
    seed: int = Field(default=20260923, ge=0, le=4294967295, strict=True)


    @model_validator(mode='after')
    def validate_voice_mode(self):
        if self.mode == 'reference':
            if not self.voice_reference or not self.voice_reference.strip() or self.voice_description is not None:
                raise ValueError('参考模式须提供音色参考，不能同时提供声音设计描述')
        elif not self.voice_description or not self.voice_description.strip() or self.voice_reference is not None:
            raise ValueError('设计模式须提供声音描述，不能同时提供参考音频')
        return self


class MediaOperationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['silence', 'video_qc', 'transcribe', 'silent_video']
    idempotency_key: str = Field(min_length=1, max_length=160)
    source_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    duration_seconds: float | None = Field(default=None, gt=0, le=120, strict=True, allow_inf_nan=False)
    sample_rate: Literal[16000, 24000, 48000] = 24000
    channels: Literal[1, 2] = 1
    sample_count: int = Field(default=12, ge=3, le=60, strict=True)
    expected_text: str = Field(default='', max_length=2000)
    language: Literal['zh', 'en', 'auto'] = 'zh'
    word_timestamps: StrictBool = True

    @field_validator('sample_rate', 'channels', mode='before')
    @classmethod
    def integer_audio_format(cls, value):
        if type(value) is not int:
            raise ValueError('采样率和声道须为整数')
        return value

    @model_validator(mode='after')
    def operation_fields(self):
        fields = {
            'silence': {'duration_seconds', 'sample_rate', 'channels'},
            'video_qc': {'sample_count'},
            'transcribe': {'expected_text', 'language', 'word_timestamps'},
            'silent_video': set(),
        }[self.operation]
        extra = self.model_fields_set - fields - {'operation', 'idempotency_key', 'source_asset_id'}
        if extra:
            raise ValueError('此操作不接受参数: ' + ', '.join(sorted(extra)))
        if not self.idempotency_key.strip():
            raise ValueError('提交凭据不能为空')
        if self.operation == 'silence':
            if self.source_asset_id is not None or self.duration_seconds is None:
                raise ValueError('静音生成须指定时长且不接收源素材')
            if type(self.sample_rate) is not int or type(self.channels) is not int:
                raise ValueError('采样率和声道须为整数')
        elif self.source_asset_id is None or not self.source_asset_id.strip():
            raise ValueError('请选择本作品的源素材')
        return self

    def parameters(self) -> dict[str, Any]:
        fields = {'silence': ('duration_seconds', 'sample_rate', 'channels'),
                  'video_qc': ('sample_count',),
                  'transcribe': ('expected_text', 'language', 'word_timestamps'),
                  'silent_video': ()}[self.operation]
        return {field: getattr(self, field) for field in fields}


class AudioEditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_asset_id: str = Field(min_length=1, max_length=160)
    background_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    start_seconds: float = Field(default=0, ge=0, le=86400, strict=True, allow_inf_nan=False)
    duration_seconds: float | None = Field(default=None, gt=0, le=120, strict=True, allow_inf_nan=False)
    pad_silence: StrictBool = False
    gain: float = Field(default=1, ge=0, le=8, strict=True, allow_inf_nan=False)
    fade_in_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)
    fade_out_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)
    background_gain: float = Field(default=.25, ge=0, le=8, strict=True, allow_inf_nan=False)
    background_offset_seconds: float = Field(default=0, ge=0, le=120, strict=True, allow_inf_nan=False)


class AudioDuckingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    threshold_db: float = Field(default=-24, ge=-60, le=0, strict=True, allow_inf_nan=False)
    ratio: float = Field(default=4, ge=1, le=20, strict=True, allow_inf_nan=False)
    attack_seconds: float = Field(default=.01, ge=.001, le=1, strict=True, allow_inf_nan=False)
    release_seconds: float = Field(default=.2, ge=.01, le=2, strict=True, allow_inf_nan=False)


class AudioMasterRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_asset_id: str = Field(min_length=1, max_length=160)
    background_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    target_lufs: float = Field(default=-16, ge=-24, le=-8, strict=True, allow_inf_nan=False)
    true_peak_dbtp: float = Field(default=-1, ge=-3, le=0, strict=True, allow_inf_nan=False)
    fade_in_seconds: float = Field(default=0, ge=0, le=10, strict=True, allow_inf_nan=False)
    fade_out_seconds: float = Field(default=0, ge=0, le=10, strict=True, allow_inf_nan=False)
    trim_start_seconds: float = Field(default=0, ge=0, le=3600, strict=True, allow_inf_nan=False)
    trim_duration_seconds: float | None = Field(default=None, gt=0, le=1800, strict=True, allow_inf_nan=False)
    background_gain_db: float = Field(default=-12, ge=-60, le=12, strict=True, allow_inf_nan=False)
    ducking: AudioDuckingRequest | None = None
    pad_silence: StrictBool = False


class AssemblyJoinReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    status: Literal['pending', 'approved', 'redo_left', 'redo_right']
    constraint: str = Field(default='', max_length=2000)
    note: str = Field(default='', max_length=2000)


class AssemblySoundRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    policy: Literal['segment_native', 'complete_master', 'mute']
    audio_asset_id: str | None = Field(default=None, max_length=160)


class AssemblyRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)


class MissingExecutionResolution(BaseModel):
    confirm_execution_ended: Literal[True]
    note: str = Field(min_length=1, max_length=2000)
