"""命名设定库与预设库的 HTTP 入口：列出、保存、删除，全部由用户显式操作。"""
from __future__ import annotations

import time
from typing import Any
from fastapi import APIRouter, HTTPException

from .. import creative_settings, preset_library
from ..schemas import CreativeSettingRequest
from .. import app as backend


ROUTER = APIRouter()


@ROUTER.get('/api/creative-settings')
def list_creative_settings() -> dict[str, Any]:
    try:
        return {'settings': creative_settings.load()}
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@ROUTER.put('/api/creative-settings/{kind}/{name}')
def save_creative_setting(kind: str, name: str, request: CreativeSettingRequest) -> dict[str, Any]:
    try:
        if kind not in creative_settings.KINDS:
            raise HTTPException(422, '未知设定分区')
        return {'entry': creative_settings.save(kind, name, request.params, now=time.time())}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.delete('/api/creative-settings/{kind}/{name}')
def delete_creative_setting(kind: str, name: str) -> dict[str, Any]:
    try:
        if kind not in creative_settings.KINDS:
            raise HTTPException(422, '未知设定分区')
        removed = creative_settings.remove(kind, name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not removed:
        raise HTTPException(404, '没有找到这条设定，可能已被删除')
    return {'removed': True}


@ROUTER.get('/api/preset-library')
def list_preset_library() -> dict[str, Any]:
    """内置起点卡片 + 你自己存的卡片；同名时你的那张替换内置那张。"""
    try:
        return {'library': {kind: preset_library.cards(kind) for kind in preset_library.KINDS}}
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@ROUTER.put('/api/preset-library/{kind}/{name}')
def save_preset_card(kind: str, name: str, request: CreativeSettingRequest) -> dict[str, Any]:
    try:
        return {'entry': preset_library.save(kind, name, request.params, now=time.time())}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.delete('/api/preset-library/{kind}/{name}')
def delete_preset_card(kind: str, name: str) -> dict[str, Any]:
    try:
        removed = preset_library.remove(kind, name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not removed:
        raise HTTPException(404, '没有找到这张卡片，可能已被删除')
    return {'removed': True}
