"""命名设定库与预设库的 HTTP 入口：列出、保存、删除，全部由用户显式操作。"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import creative_settings, preset_library
from ..schemas import CreativeSettingRequest, PresetRenameRequest
from .. import app as backend

# 小样要能被 /media-file 播放：只有 input/output/workspaces/assets 这些登记根才放行，
# 而 config/ 不在其中（以前存这里，卡片上的播放器其实一直是 404）。
MUSIC_SAMPLE_DIR = '配乐库'


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
        return {'entry': preset_library.save(kind, name, request.params, now=time.time(),
                                             source_task_id=request.source_task_id)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.post('/api/preset-library/{kind}/{name}/rename')
def rename_preset_card(kind: str, name: str, request: PresetRenameRequest) -> dict[str, Any]:
    """改自己保存的卡片的名字；内置卡片没有可改的条目，会明确报错。"""
    try:
        return preset_library.rename(kind, name, request.name)
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


class SaveMusicSampleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=60)


def _music_result_path(task: dict[str, Any]) -> Path | None:
    result = task.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    result = result if isinstance(result, dict) else {}
    raw = result.get('output')
    if not raw:
        listed = result.get('outputs') or []
        raw = listed[0] if listed else ''
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    candidate = Path(text)
    if not candidate.is_absolute():
        # ComfyUI 的相对写法是 output/<子路径>；服务端这两个前缀都见过。
        relative = text[7:] if text.replace('\\', '/').startswith('output/') else text
        candidate = Path(backend.OUTPUT_ROOT) / relative
    return candidate


@ROUTER.post('/api/preset-library/music/sample')
def save_music_sample(request: SaveMusicSampleRequest) -> dict[str, Any]:
    """把一条配乐任务的成品音频存进配乐库。

    音频会**复制**到预置库目录：之后删掉任务、清空输出目录都不影响这张卡。
    """
    task = backend.get_task(request.task_id)
    if not task:
        raise HTTPException(404, '任务不存在，可能已被删除')
    payload = task.get('payload') or {}
    if payload.get('kind') != 'music':
        raise HTTPException(422, '这条任务不是配乐任务')
    if task.get('status') != 'succeeded':
        raise HTTPException(409, '这条配乐还没生成成功')
    source = _music_result_path(task)
    if not source or not source.is_file():
        raise HTTPException(409, '成品音频文件已不在，无法存入配乐库')
    folder = Path(backend.ASSETS_ROOT) / MUSIC_SAMPLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{uuid.uuid4().hex}{source.suffix.lower() or ".mp3"}'
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise HTTPException(500, f'复制音频失败：{exc}') from exc
    frozen = payload.get('music') or {}
    params: dict[str, Any] = {key: frozen[key] for key in
                              ('tags', 'bpm', 'keyscale', 'timesignature', 'duration_seconds', 'language')
                              if frozen.get(key) is not None}
    params.setdefault('tags', '成品配乐')
    params.setdefault('bpm', 90)
    params.setdefault('keyscale', 'C major')
    params.setdefault('timesignature', '4')
    params.setdefault('duration_seconds', 30)
    params.setdefault('language', 'unknown')
    # assets 根不在 root_relative_path 的白名单里，这里直接存绝对路径；
    # /media-file 的 allowed 列表包含 ASSETS_ROOT，能正常读出。
    params['sample_path'] = str(target.resolve())
    if frozen.get('duration_seconds'):
        params['sample_duration_seconds'] = float(frozen['duration_seconds'])
    try:
        entry = preset_library.save('music', request.name, params, now=time.time(),
                                    source_task_id=request.task_id)
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    return {'entry': entry}
