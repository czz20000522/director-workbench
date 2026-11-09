"""提示词的两个半边：代写（走本机模型）与词库（只走本机文件）。

代写只发用户自己那句想法和当前选的素材标题，不读工程、不传图片；
词库读写 ``director-data/config/prompt-library.json``，都不经过云端。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import app as backend
from .. import prompt_library, prompt_writer

ROUTER = APIRouter()


class PromptDraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    idea: str = Field(min_length=1, max_length=prompt_writer.MAX_IDEA)
    # auto = 用配置里的默认通道；显式传 local/online 时按用户这次点的来。
    provider: Literal['auto', 'local', 'online'] = 'auto'
    # compose = 一句话写成描述；polish = 把用户自己写的这段改好。
    mode: Literal['compose', 'polish'] = 'compose'
    temperature: float = Field(default=0.9, ge=0, le=1.5, strict=True, allow_inf_nan=False)
    # 当前工作台的选择：素材槽标题、画质档、画幅。只用来让写出来的描述对得上手里的图。
    context: dict[str, Any] | None = None

    @field_validator('idea')
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('先写一句想法')
        return value.strip()


class LocalSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str | None = Field(default=None, max_length=120)
    keep_alive: str | None = Field(default=None, max_length=16)


class OnlineSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str | None = Field(default=None, max_length=400)
    model: str | None = Field(default=None, max_length=120)
    # None = 不改；空串 = 清除；其它 = 覆盖。
    api_key: str | None = Field(default=None, max_length=400)


class PromptConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['local', 'online'] | None = None
    local: LocalSettings | None = None
    online: OnlineSettings | None = None


class DescribeImageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image_path: str = Field(min_length=1, max_length=1000)
    provider: Literal['local', 'online'] = 'local'


class PromptSaveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=prompt_library.MAX_TEXT)
    title: str | None = Field(default=None, max_length=prompt_library.MAX_TITLE)
    tags: list[str] | None = None
    note: str | None = None
    source: Literal['manual', 'draft', 'generation', 'import'] = 'manual'
    idea: str = Field(default='', max_length=prompt_writer.MAX_IDEA)
    model: str = Field(default='', max_length=80)
    links: dict[str, Any] | None = None


class PromptUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(default=None, max_length=prompt_library.MAX_TITLE)
    tags: list[str] | None = None
    note: str | None = Field(default=None, max_length=prompt_library.MAX_NOTE)


class PromptUseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    result: str = Field(default='', max_length=1000)
    task_id: str = Field(default='', max_length=200)


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(404, str(exc))


@ROUTER.get('/api/prompt-writer')
def prompt_writer_capability() -> dict[str, Any]:
    return prompt_writer.capability()


@ROUTER.post('/api/prompt-writer/draft')
def prompt_draft(request: 'PromptDraftRequest') -> dict[str, Any]:
    try:
        return prompt_writer.draft(request.idea, provider=request.provider,
                                   temperature=request.temperature, context=request.context,
                                   mode=request.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except prompt_writer.WriterUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except prompt_writer.WriterFailed as exc:
        raise HTTPException(502, str(exc)) from exc


@ROUTER.get('/api/prompt-writer/config')
def prompt_writer_config() -> dict[str, Any]:
    return prompt_writer.config_view()


@ROUTER.post('/api/prompt-writer/describe-image')
def prompt_describe_image(request: 'DescribeImageRequest') -> dict[str, Any]:
    """图片 → 提示词：给「参考图生成」的第一步用。"""
    try:
        return prompt_writer.describe_image(request.image_path, provider=request.provider)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except prompt_writer.WriterUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except prompt_writer.WriterFailed as exc:
        raise HTTPException(502, str(exc)) from exc


@ROUTER.post('/api/prompt-writer/config')
def prompt_writer_save_config(request: 'PromptConfigRequest') -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if request.provider:
        patch['provider'] = request.provider
    if request.local is not None:
        patch['local'] = request.local.model_dump(exclude_none=True)
    if request.online is not None:
        patch['online'] = request.online.model_dump(exclude_none=True)
    try:
        prompt_writer.save_config(patch)
    except prompt_writer.WriterFailed as exc:
        raise HTTPException(500, str(exc)) from exc
    return prompt_writer.config_view()


@ROUTER.post('/api/prompt-writer/release')
def prompt_writer_release() -> dict[str, Any]:
    """出片前主动退显存；卸不掉也返回 200，让调用方继续。"""
    return prompt_writer.release()


@ROUTER.get('/api/prompt-library')
def prompt_library_list(query: str = '', tag: str = '', project_id: str = '') -> dict[str, Any]:
    try:
        return prompt_library.search(query=query, tag=tag, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@ROUTER.post('/api/prompt-library')
def prompt_library_create(request: 'PromptSaveRequest') -> dict[str, Any]:
    try:
        return prompt_library.create(request.text, title=request.title, tags=request.tags,
                                     source=request.source, idea=request.idea,
                                     model=request.model, note=request.note, links=request.links)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.patch('/api/prompt-library/{item_id}')
def prompt_library_update(item_id: str, request: 'PromptUpdateRequest') -> dict[str, Any]:
    try:
        return {'item': prompt_library.update(item_id, title=request.title, tags=request.tags,
                                              note=request.note)}
    except KeyError as exc:
        raise backend._conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.delete('/api/prompt-library/{item_id}')
def prompt_library_delete(item_id: str) -> dict[str, Any]:
    try:
        removed = prompt_library.remove(item_id)
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc
    if not removed:
        raise HTTPException(404, '提示词库里没有这一条')
    return {'removed': True}


@ROUTER.post('/api/prompt-library/{item_id}/use')
def prompt_library_use(item_id: str, request: 'PromptUseRequest') -> dict[str, Any]:
    try:
        return {'item': prompt_library.mark_used(item_id, result=request.result,
                                                task_id=request.task_id)}
    except KeyError as exc:
        raise backend._conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
