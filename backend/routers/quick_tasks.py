"""单步任务：不属于任何作品的快速生成，以及跨作品的批量整理。

用户要的是「我就想试一下，生成点什么」——没有作品主题，也不该在项目列表里留下
痕迹。服务端用一个隐藏容器 ``quick-tasks`` 承载任务行、冻结快照和产物归属，因为
后端到处假设任务有 project_id；这个容器从不出现在 /api/projects 里，用户看到的
只是一条条以提示词开头命名的单步任务。

批量删除沿用作品删除的同一份两段式合同：预览冻结范围，apply 必须显式确认，
状态或范围与预览不一致就拒绝。11-07 起被清除的快照与产物送进 Windows 回收站，
不再永久删除；被其它任务引用的产物、归档区、模型文件一律保留。
"""
from __future__ import annotations

import json
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import app as backend
from .. import generated_orphans as orphan_scan
from .. import library_archive, preset_library, production_presets, trash
from .. import task_scheduler
from ..schemas import QuickTaskAdoptRequest, TaskBatchDeleteRequest, TaskBatchPlanRequest


ROUTER = APIRouter()

QUICK_PROJECT_ID = 'quick-tasks'
QUICK_SERIES = '单步任务'
QUICK_TITLE = '单步任务'
QUICK_WORKSPACE_DIRECTORY = '_单步任务'
TASK_PLAN_TTL_SECONDS = 20 * 60
TOOL_LABELS = {'keyframe': '图片', 'workflow': '视频', 'enhancement': '清晰化', 'media_operation': '媒体处理', 'music': '配乐',
               'video_edit': '剪辑', 'audio_edit': '音频编辑', 'audio_master': '全片母版', 'speech': '配音',
               'anchor_dubbing': '锚点配音', 'audio_swap': '换音轨', 'vlm_observation': '画面观察',
               'reference_decomposition': '参考拆解', 'reference_semantic': '参考分析'}


def task_plan_directory() -> Path:
    return backend.RUNTIME / 'task-deletion-plans'


def ensure_quick_project() -> str:
    """Create the hidden quick container once, then hand back its id."""
    try:
        backend.load_project_manifest(QUICK_PROJECT_ID)
        return QUICK_PROJECT_ID
    except RuntimeError:
        pass
    workspace = (backend.DIRECTOR_WORKSPACES_ROOT / QUICK_WORKSPACE_DIRECTORY).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    for folder in ('plans', 'assets/image', 'assets/audio', 'assets/video', 'workflows/ui', 'workflows/api',
                   'records/prompts', 'records/tasks', 'records/reviews', 'keyframe-tasks'):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    manifest = backend.blank_project_manifest(QUICK_PROJECT_ID, QUICK_SERIES, QUICK_TITLE, workspace)
    backend.persist_project_manifest(manifest, make_default=False)
    return QUICK_PROJECT_ID


def task_tool(payload: dict[str, Any]) -> str:
    """配音 that starts from a written timbre description is the 音色设计 tool, not a dialogue take."""
    if backend.is_enhancement(payload):
        return '清晰化'
    # 局部重绘也走 keyframe 图，但用户心里它是「改图」不是「文生图」：
    # 混在文生图堆里，昨天的重绘任务看起来就像一个没写好的出图任务。
    if payload.get('kind') == 'keyframe' and (payload.get('request') or {}).get('mode') == 'inpaint':
        return '局部重绘'
    # 用参考图拼出来的首帧不是"文生图"：起点是素材库里的图和人脸/服装/姿势槽。
    # 混在文生图堆里，用户会以为这些图是纯文字生成的，也找不到该去哪改。
    if payload.get('kind') == 'keyframe' and (payload.get('request') or {}).get('mode') == 'reference':
        return '参考图生图'
    if payload.get('kind') == 'speech' and (payload.get('request') or {}).get('mode') == 'design':
        return '音色设计'
    # 视频任务走的是作品管线，payload 里没有 kind，只有 pipeline_stage_id。
    # 以前这里落到兜底的「任务」，首页就把图生视频那条丢进「其他」。
    if payload.get('kind') is None and payload.get('pipeline_stage_id'):
        return '视频'
    return TOOL_LABELS.get(str(payload.get('kind')), '任务')


def _first_frame_of(payload: dict[str, Any]) -> str | None:
    """Which frame did this video start from?  Three payload shapes exist,
    because the runner has been through three generations of submission code."""
    direct = payload.get('first_frame')
    if isinstance(direct, str) and direct:
        return direct
    materials = payload.get('materials')
    if isinstance(materials, dict) and isinstance(materials.get('first-frame'), str):
        return materials['first-frame']
    values = payload.get('pipeline_values')
    if isinstance(values, dict) and isinstance(values.get('first-frame'), str):
        return values['first-frame']
    snapshot = payload.get('segment_snapshot')
    if isinstance(snapshot, dict):
        keyframes = snapshot.get('keyframes')
        if isinstance(keyframes, dict) and isinstance(keyframes.get('first_frame'), str):
            return keyframes['first_frame']
    return None


def is_enhancement(payload: dict[str, Any]) -> bool:
    """清晰化 also runs a ComfyUI graph, so its payload kind says 'workflow'."""
    return bool(payload.get('enhancement')) or payload.get('pipeline_stage_id') == 'media-enhancement'


def _portable(value: Any) -> str | None:
    """One path form for every reuse value, matching what the panels already accept."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return backend.root_relative_path(backend.resolve_registered_path(value))
    except (ValueError, OSError, HTTPException):
        return None


def _aspect(width: Any, height: Any) -> str | None:
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    return '1:1' if width == height else ('16:9' if width > height else '9:16')


MUSIC_FORM_FIELDS = ('tags', 'lyrics', 'duration_seconds', 'bpm', 'keyscale', 'timesignature', 'language',
                     'steps', 'cfg', 'shift', 'quality', 'generate_audio_codes', 'encode_cfg_scale',
                     'temperature', 'top_p', 'top_k', 'min_p')


def quick_task_reuse(payload: dict[str, Any], produced: str = '') -> dict[str, Any] | None:
    """The editable inputs of a finished single-step task, in the tool form's own field names.

    首页「编辑重生成」把这些值带回对应工具，由你改完再显式提交——工作台不会自动重跑。
    带不全的工具就不给这个入口：表单看着像上次、其实不是，比没有入口更糟。
    """
    if backend.is_enhancement(payload):
        source = backend._portable((payload.get('enhancement') or {}).get('source'))
        return {'tool': 'enhance', 'values': {'source': source}} if source else None
    kind = str(payload.get('kind') or '')
    if kind == 'keyframe':
        request = payload.get('request') or {}
        prompt = str(request.get('prompt') or '').strip()
        if not prompt:
            return None
        size = payload.get('output_size') or {}
        values: dict[str, Any] = {'prompt': prompt,
                                  'quality': str(request.get('output_resolution') or size.get('resolution') or 'native')}
        if type(request.get('seed')) is int:
            values['seed'] = request['seed']
        if type(request.get('steps')) is int:
            values['steps'] = request['steps']
        aspect = backend._aspect(request.get('width'), request.get('height'))
        if aspect:
            values['aspect'] = aspect
        if request.get('mode') == 'reference':
            # 参考图任务有两种来路，编辑时要回各自那一页，别串台：
            #   1. 首帧工作台拼的槽位（人脸/造型/服装/姿势）→ 回「图生视频」的素材组合；
            #   2. 参考图生成工具的一张底图 → 回「参考图生成」，图和词一起放回去。
            # 判据只能看提交时的原话：冻结后的槽只留副本路径，material_id 丢了。
            submitted = ((payload.get('submission_receipt') or {}).get('request') or {})
            picked = [item for item in (submitted.get('references') or [])
                      if isinstance(item, dict) and str(item.get('material_id') or '').strip()]
            if not picked:
                # 没有提交回执的旧记录：冻结槽里也可能还带 material_id，照样认。
                picked = [item for item in (request.get('references') or [])
                          if isinstance(item, dict) and str(item.get('material_id') or '').strip()]
            if picked:
                # 冻结槽里的中文标题还能用，按槽名贴回去，界面先显示名字。
                labels = {str(item.get('role') or ''): str(item.get('label') or '')
                          for item in (request.get('references') or []) if isinstance(item, dict)}
                slots = []
                for item in picked:
                    role = str(item.get('role') or '').strip()
                    if not role:
                        continue
                    slots.append({'role': role, 'material_id': str(item['material_id']),
                                  'label': labels.get(role, '')})
                if slots:
                    values['references'] = slots
                    if request.get('creative_profile'):
                        values['profile'] = str(request['creative_profile'])
                    return {'tool': 'image-video', 'values': values}
            # 只有一张底图：参考图生成的产物，或者首帧台只选了「自有素材」。
            # 两者都是一张图加一段词，回参考图生成页最顺手。
            frozen = [str(item.get('path') or '') for item in (request.get('references') or [])
                      if isinstance(item, dict)]
            image = next((path for path in frozen if path), '') or str(request.get('reference_image') or '')
            portable = backend._portable(image)
            if not portable:
                # 不知道当时用了什么图，带回去也是个空表单——比没有入口更糟。
                return None
            values['image'] = portable
            if type(request.get('refine')) is bool:
                values['refine'] = request['refine']  # 面板上叫「降噪」，保持原样
            return {'tool': 'reference-image', 'values': values}
        return {'tool': 'image', 'values': values}
    if kind == 'workflow' or (not kind and isinstance(payload.get('segment_snapshot'), dict)):
        # 视频任务走作品管线，payload 里没有 kind，只有 pipeline_stage_id；以前只认
        # kind=='workflow'，于是文生视频、图生视频卡片一直没有「编辑重生成」。
        snapshot = payload.get('segment_snapshot') or {}
        generation = snapshot.get('generation') or {}
        prompt = str(payload.get('prompt_override') or payload.get('prompt') or snapshot.get('prompt') or '').strip()
        if not prompt:
            return None
        values = {'prompt': prompt}
        frame = backend._portable(payload.get('first_frame') or (snapshot.get('keyframes') or {}).get('first'))
        if frame:
            values['frame'] = frame
        duration = snapshot.get('duration_seconds')
        if type(duration) in (int, float) and duration > 0:
            values['duration'] = int(duration) if float(duration).is_integer() else float(duration)
        if generation.get('aspect_ratio') in ('1:1', '16:9', '9:16'):
            values['aspect'] = generation['aspect_ratio']
        if generation.get('output_resolution'):
            values['quality'] = str(generation['output_resolution'])
        # 视频工具必须显式选制作方案：把这条任务当时的方案一起带回去，否则表单看着
        # 像上次，实际按另一条路线重跑（画幅、步数、LoRA 都可能不同）。
        stage = str(payload.get('pipeline_stage_id') or '')
        preset = next((item for item in production_presets.PRESET_IDS
                       if production_presets.stage_id(item) == stage), None)
        if preset:
            values['preset'] = preset
        # 关联过的声音也一起带回去：改提示词重跑时不该把配音和配乐丢回「不关联」。
        guide = backend._portable((snapshot.get('audio') or {}).get('guide'))
        if guide:
            values['voice'] = guide
        music = backend._portable(payload.get('music_overlay'))
        if music:
            values['music_track'] = music
        # 上一版成片也带回去：重编辑时右栏能继续播放，台词工位的「上一段音轨」也才有来源。
        clip = backend._portable(produced)
        if clip:
            values['video'] = clip
        return {'tool': 'image-video' if frame else 'text-video', 'values': values}
    if kind == 'speech':
        request = payload.get('request') or {}
        if request.get('mode') != 'design':
            return None  # 克隆模式的起点是一段参考音频，得你自己重新选
        description = str(request.get('voice_description') or '').strip()
        if not description:
            return None
        values = {'timbre': description, 'text': str(request.get('text') or '')}
        seed = (request.get('parameters') or {}).get('seed')
        if type(seed) is int:
            values['seed'] = seed
        return {'tool': 'timbre', 'values': values}
    if kind == 'music':
        frozen = payload.get('music') or {}
        if not str(frozen.get('tags') or '').strip():
            return None
        values = {'music': {key: frozen[key] for key in MUSIC_FORM_FIELDS if key in frozen}}
        if frozen.get('format'):
            values['music']['output_format'] = frozen['format']  # 表单里叫 output_format
        if type(frozen.get('seed')) is int:
            values['seed'] = frozen['seed']
        return {'tool': 'music', 'values': values}
    return None


def quick_prompt_excerpt(task: dict[str, Any], length: int = 46) -> str:
    payload = task.get('payload') or {}
    if payload.get('kind') == 'video_edit':
        # 剪辑任务没有提示词；用段数和总时长当标题，列表里才分得清是哪一条。
        request = payload.get('request') or {}
        clips = request.get('clips') or []
        total = 0.0
        for clip in clips:
            try:
                total += max(0.0, float(clip.get('end_seconds') or 0) - float(clip.get('start_seconds') or 0))
            except (TypeError, ValueError):
                continue
        return f'剪辑 · {len(clips)} 段 · {total:.1f} 秒' if clips else '剪辑'
    prompt = backend._prompt_of(task)
    if not prompt and backend.is_enhancement(payload):
        recipe = payload.get('enhancement') or {}
        # 清晰化任务没有提示词，标题取提交时冻结的 label；老任务退回文件名词干。
        prompt = str(recipe.get('label') or Path(str(recipe.get('source') or '')).stem)
    collapsed = ' '.join(prompt.split())
    if not collapsed:
        return backend.task_tool(payload)
    return collapsed if len(collapsed) <= length else collapsed[:length].rstrip() + '…'


def quick_prompt_full(task: dict[str, Any]) -> str:
    """完整提示词：不截断、只把连续空白压成一个空格。

    ``quick_prompt_excerpt`` 是列表标题（46 字 + 省略号），结果预览要用全文。
    """
    payload = task.get('payload') or {}
    if payload.get('kind') == 'video_edit':
        return ''
    prompt = backend._prompt_of(task)
    if not prompt and backend.is_enhancement(payload):
        recipe = payload.get('enhancement') or {}
        prompt = str(recipe.get('label') or Path(str(recipe.get('source') or '')).stem)
    return ' '.join(str(prompt).split())


def _card_outputs(result: dict[str, Any]) -> list[str]:
    """任务产物有两种写法，不能混：

    - ComfyUI 视频任务：`outputs` 是相对 output 目录的字符串（要补 `output/`）；
    - 媒体/剪辑/音频任务：`outputs` 是 `{kind, path, ...}` 对象，路径已经是可用的
      绝对路径或登记路径，再拼前缀会得到 ``output/{'path': ...}`` 这种坏串。
    """
    published = result.get('published_outputs')
    if published:
        return [str(item) for item in published]
    outputs: list[str] = []
    for item in result.get('outputs') or []:
        if isinstance(item, dict):
            path = item.get('path')
            if isinstance(path, str) and path:
                outputs.append(path)
        elif isinstance(item, str) and item:
            outputs.append(f'output/{item}')
    # 关键帧/清晰化把唯一产物放在 output（视频另有 video_path），没有 outputs 列表。
    single = result.get('output') or result.get('video_path')
    if isinstance(single, str) and single:
        # 音频/媒体任务会同时写 output 和 outputs，而两边的写法不一样：
        # output 是绝对路径，outputs 里是 `output/<相对路径>`。直接比字符串会判成两个文件，
        # 结果弹窗就出现两个一模一样的标签（同一段音频被列了两次）。
        def _same(left: str, right: str) -> bool:
            def norm(value: str) -> str:
                text = value.replace('\\', '/').lstrip('/')
                return text[7:] if text.startswith('output/') else text
            return norm(left) == norm(right) or norm(left).endswith('/' + norm(right)) or norm(right).endswith('/' + norm(left))
        if not any(_same(single, item) for item in outputs):
            outputs.insert(0, single)
    return outputs


def quick_task_card(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get('payload') or {}
    result = task.get('result') or {}
    tool = backend.task_tool(payload)
    outputs = _card_outputs(result or {})
    if not outputs and isinstance((result or {}).get('output'), str) and result['output']:
        # 关键帧任务把产物写在 output（绝对路径）里，不是 outputs。
        outputs = [result['output']]
    return {
        'id': task['id'], 'kind': payload.get('kind'),
        'tool': tool,
        # 视频任务再分文生/图生：有首帧就是图生视频，没有就是文生视频。
        # 首帧可能写在 first_frame、materials 或 pipeline_values 里，三者都要认。
        'video_mode': (('image-video' if backend._first_frame_of(payload) else 'text-video') if tool == '视频' else None),
        'title': backend.quick_prompt_excerpt(task),
        # 列表用 46 字摘要，但「查看结果」那张弹窗要把整段提示词显示出来，
        # 所以单独给一份不截断的。剪辑/清晰化这类没有提示词的任务返回空串。
        'prompt': quick_prompt_full(task),
        'status': task['status'],
        'created_at': task.get('created_at'), 'updated_at': task.get('updated_at'),
        # 未读/已读：生成成功但用户还没打开过结果的，标成未读；失败/在途不算。
        'read_at': task.get('read_at'),
        'unread': task.get('status') == 'succeeded' and not task.get('read_at'),
        # 耗时：终态是「花了多久」，运行中是「已用多久」（首页每 2 秒刷新一次）。
        'started_at': task.get('started_at'),
        'duration_ms': backend.quick_task_duration_ms(task),
        # 视频/音乐这类任务把产物写在 outputs 里，只有清晰化曾经单独处理。
        # 现在统一取：单产物直接给 output，多产物留给前端列清单。
        'output': ((result or {}).get('output') or (result or {}).get('video_path')
                   or (outputs[0] if len(outputs) == 1 else None)),
        'outputs': outputs,
        'error': task.get('error'), 'revision': payload.get('revision', 0),
        'retry_of': payload.get('retry_of'), 'queue': task.get('queue') or {},
        'progress': backend.comfy_progress.visible_progress(task),
        'reuse': backend.quick_task_reuse(payload, outputs[0] if outputs else ''),
        'deletable': backend._delete_blocker(task) is None,
        'delete_blocker': backend._delete_blocker(task),
    }


@ROUTER.get('/api/quick-workspace')
def quick_workspace() -> dict[str, Any]:
    """The container id single-step tools submit against; created on first use."""
    return {'project_id': backend.ensure_quick_project()}


def adopt_task_outputs(task: dict[str, Any], project_id: str, paths: list[str] | None = None) -> dict[str, Any]:
    """把一条草稿任务的产物复制并登记成作品资产（核心，路由与执行器共用）。

    调用方负责校验任务状态与来源；这里只管「复制 + 登记 + 幂等」，出错抛 ValueError。
    """
    with backend.MATERIAL_REUSE_LOCK:
        task_id = str(task['id'])
        manifest = backend.load_project_manifest(project_id)
        outputs = _card_outputs(task.get('result') or {})
        wanted = [item for item in (paths or []) if item in outputs] if paths else outputs
        if not wanted:
            raise ValueError('这条任务没有可归入的产物')
        workspace = (backend.resolve_registered_path(str(manifest['workspace_root']))
                     if manifest.get('workspace_root')
                     else (backend.DIRECTOR_WORKSPACES_ROOT / project_id).resolve())
        assets = list(manifest.get('assets') or [])
        title = quick_prompt_excerpt(task, 28) or backend.task_tool(task.get('payload') or {})
        tool = backend.task_tool(task.get('payload') or {})
        registered: list[dict[str, Any]] = []
        created = 0
        skipped = 0
        for value in wanted:
            try:
                source_path = backend.resolve_registered_path(value)
                if not source_path.is_file():
                    raise ValueError('产物文件不存在')
            except (ValueError, OSError) as exc:
                raise ValueError(f'产物不可读取：{exc}') from exc
            provenance = {'task_id': task_id, 'path': str(source_path)}
            existing = next((asset for asset in assets if isinstance(asset, dict)
                             and (asset.get('adopted_from') or {}).get('task_id') == task_id
                             and (asset.get('adopted_from') or {}).get('path') == str(source_path)
                             and backend._asset_record_exists(asset)), None)
            if existing:
                registered.append(existing)
                skipped += 1
                continue
            destination = (workspace / 'assets' / 'adopted' / uuid.uuid4().hex / source_path.name).resolve()
            if not destination.is_relative_to(workspace.resolve()):
                raise ValueError('目标素材目录越界')
            try:
                destination.parent.mkdir(parents=True, exist_ok=False)
                before = source_path.stat()
                shutil.copy2(source_path, destination)
                after = source_path.stat()
            except OSError as exc:
                raise ValueError('产物复制失败，请确认草稿文件与目标目录仍可访问后重试') from exc
            if (destination.stat().st_size != before.st_size
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise ValueError('产物在复制期间发生变化，请重试')
            kind = backend.material_kind(destination)
            relative = backend.root_relative_path(destination)
            record = {'id': f'adopt-{uuid.uuid4().hex[:16]}', 'kind': kind, 'title': title,
                      'ready': True, 'origin': f'单步任务·{tool} 归入',
                      'subtitle': '待本作品审核',
                      'adopted_from': {**provenance, 'tool': tool, 'copied_at': time.time()}}
            if kind == 'image':
                record['image_path'] = relative
            else:
                record['sources'] = {'A': relative}
            assets.append(record)
            registered.append(record)
            created += 1
        if created:
            manifest['assets'] = assets
            backend.save_project_manifest(manifest)
            if project_id == backend.CURRENT_PROJECT_ID:
                backend.apply_project_manifest(manifest)
        return {'project_id': project_id, 'assets': registered,
                'adopted': created, 'already_adopted': skipped}


@ROUTER.post('/api/quick-tasks/{task_id}/adopt')
def adopt_quick_task(task_id: str, request: QuickTaskAdoptRequest) -> dict[str, Any]:
    """把单步任务的产物归入某部作品。

    草稿箱（quick-tasks）里的任务是「没归属」的：产物还在 ComfyUI 输出目录，删任务
    就一起进回收站。归入某部作品 = 复制一份到该作品工作区、登记成它的资产，之后
    再删草稿也不会影响作品。同一个任务重复归入同一部作品是幂等的。
    """
    backend.ensure_quick_project()
    task = backend.get_task(task_id)
    if not task or str(task.get('project_id') or '') != QUICK_PROJECT_ID:
        raise HTTPException(404, '单步任务不存在')
    if task.get('status') != 'succeeded':
        raise HTTPException(409, '任务还没成功完成，暂不能归入作品')
    if request.project_id == QUICK_PROJECT_ID:
        raise HTTPException(422, '请选择一部正式作品，草稿箱不能作为归入目标')
    try:
        return adopt_task_outputs(task, request.project_id, request.paths or None)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@ROUTER.get('/api/quick-tasks')
def quick_task_list(limit: int = Query(default=60, ge=1, le=200)) -> dict[str, Any]:
    """List single-step tasks as their own thing, titled by the prompt that made them."""
    backend.ensure_quick_project()
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT ?',
                                  (QUICK_PROJECT_ID, limit)).fetchall()
    tasks = [backend.task_row(row) for row in rows]
    for task in tasks:
        task['queue'] = backend.TASK_SCHEDULER.queue_info(task['id'])
    return {'project_id': QUICK_PROJECT_ID,
            'tasks': [backend.quick_task_card(task) for task in tasks]}


def _batch_rows(task_ids: list[str]) -> list[dict[str, Any]]:
    with backend.DB_LOCK, backend.db() as connection:
        rows = connection.execute('SELECT * FROM tasks ORDER BY created_at DESC').fetchall()
    by_id = {str(row['id']): backend.task_row(row) for row in rows}
    selected: list[dict[str, Any]] = []
    for task_id in dict.fromkeys(task_ids):
        task = by_id.get(task_id)
        if not task:
            raise HTTPException(404, f'任务不存在：{task_id}')
        principal = backend.current_principal.get()
        if backend.PRIVATE_WORKSPACES is not None and principal is not None:
            backend.PRIVATE_WORKSPACES.verify_task(task)
        selected.append(task)
    return selected


def _task_outputs(task: dict[str, Any]) -> list[str]:
    result = task.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    if not isinstance(result, dict):
        return []
    paths: list[str] = []
    single = result.get('output') or result.get('video_path') or result.get('audio_output')
    if isinstance(single, str) and single:
        paths.append(single)
    for item in result.get('outputs') or []:
        if isinstance(item, str) and item:
            paths.append(item)
        elif isinstance(item, dict) and isinstance(item.get('path'), str):
            paths.append(item['path'])
    return paths


@ROUTER.post('/api/tasks/deletion-plan')
def tasks_deletion_plan(request: TaskBatchPlanRequest) -> dict[str, Any]:
    """Freeze which tasks would go, and what their removal touches."""
    entries = []
    for task in backend._batch_rows(request.task_ids):
        blocker = backend._delete_blocker(task)
        if blocker:
            raise HTTPException(409, f'{backend.quick_prompt_excerpt(task)}：{blocker}')
        files = backend._snapshot_files(task, str(task.get('project_id') or ''))
        entries.append({'task_id': task['id'], 'project_id': task.get('project_id'), 'status': task['status'],
                        'title': backend.quick_prompt_excerpt(task), 'asset_id': task.get('asset_id'),
                        'snapshots': [str(path) for path in files],
                        # 素材已经存进音色库/配乐库的话，这里带上库名与时间；
                        # 前端据此显示「素材已归档」，不必再勾一次归档和二次确认。
                        'archived': preset_library.archived_for_task(
                            task['id'], _task_outputs(task))})
    plan_id = secrets.token_hex(12)
    directory = backend.task_plan_directory()
    directory.mkdir(parents=True, exist_ok=True)
    created = time.time()
    # 预览计划只在「被 apply 读取」时才检查 TTL，不主动清就会一直堆在 runtime 里。
    # 每次生成新计划顺手扫一遍，只清确实过期的元数据文件。
    for stale_plan in directory.glob('*.json'):
        try:
            document = json.loads(stale_plan.read_text(encoding='utf-8'))
            if created - float(document.get('created_at', 0)) > TASK_PLAN_TTL_SECONDS:
                stale_plan.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
    rows = backend._batch_rows([entry['task_id'] for entry in entries])
    archivable: list[dict[str, Any]] = []
    for project_id in dict.fromkeys(str(row.get('project_id') or '') for row in rows):
        ids = {row['id'] for row in rows if str(row.get('project_id') or '') == project_id}
        archivable.extend(library_archive.candidates(project_id, ids))
    outputs = library_archive.owned_outputs(rows)
    retained = library_archive.retained_outputs(rows)
    backend.write_json_atomic(directory / f'{plan_id}.json',
                              {'plan_id': plan_id, 'created_at': created, 'entries': entries,
                               'archivable': archivable, 'outputs': outputs,
                               'retained_outputs': retained})
    return {'plan_id': plan_id, 'expires_at': created + TASK_PLAN_TTL_SECONDS, 'entries': entries,
            'archivable': archivable, 'outputs': outputs, 'retained_outputs': retained,
            'preserved': '模型文件、归档区与其它任务的产物不会被删除。被清除的快照与产物移入 Windows 回收站，可恢复；'
                         '被其它任务引用的产物会保留，勾选归档的会移进素材库。'}


def _load_task_plan(plan_id: str) -> dict[str, Any]:
    if not plan_id.isalnum() or len(plan_id) != 24:
        raise HTTPException(422, '删除计划编号无效')
    path = backend.task_plan_directory() / f'{plan_id}.json'
    if not path.is_file():
        raise HTTPException(404, '删除计划不存在或已过期，请重新预览')
    document = json.loads(path.read_text(encoding='utf-8'))
    if time.time() - float(document.get('created_at', 0)) > TASK_PLAN_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise HTTPException(409, '删除计划已过期，请重新预览')
    return document


@ROUTER.post('/api/tasks/deletion')
def tasks_deletion_apply(request: TaskBatchDeleteRequest) -> dict[str, Any]:
    """Delete exactly the tasks the preview listed, refusing any drift."""
    if not request.confirm:
        raise HTTPException(422, {'code': 'confirmation_required',
                                  'message': '删除任务需要显式确认；不会静默执行。'})
    document = backend._load_task_plan(request.plan_id)
    planned = {str(entry['task_id']): entry for entry in document.get('entries', [])}
    requested = list(dict.fromkeys(request.task_ids))
    if set(planned) != set(requested):
        raise HTTPException(409, '删除范围与预览时不一致，请重新预览')
    current = {task['id']: task for task in backend._batch_rows(requested)}
    for task_id, entry in planned.items():
        task = current[task_id]
        if task['status'] != entry['status']:
            raise HTTPException(409, f'{entry["title"]} 的状态已变为 {task["status"]}，请重新预览')
        blocker = backend._delete_blocker(task)
        if blocker:
            raise HTTPException(409, f'{entry["title"]}：{blocker}')

    archivable = {str(item.get('asset_id')): item for item in document.get('archivable') or []}
    chosen = [archivable[key] for key in dict.fromkeys(request.archive_asset_ids) if key in archivable]
    archived = library_archive.archive(chosen) if chosen else []
    kept_sources = {str(item.get('source')) for item in chosen}
    removed_outputs, failed_outputs = library_archive.recycle_files(
        [path for path in document.get('outputs') or [] if path not in kept_sources])
    purged_assets: dict[str, set[str]] = {}
    for item in chosen:
        purged_assets.setdefault(str(item.get('project_id') or ''), set()).add(str(item.get('asset_id') or ''))
    deleted: list[str] = []
    removed: list[str] = []
    failed_snapshots: list[str] = []
    for task_id in requested:
        task = current[task_id]
        entry = planned[task_id]
        if task['status'] == 'scheduler_waiting':
            backend.TASK_SCHEDULER.cancel_pending(task_id)
        project_id = str(task.get('project_id') or '')
        stale = backend._unreferenced([Path(path) for path in entry['snapshots']], set(requested))
        with backend.DB_LOCK, backend.db() as connection:
            connection.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            connection.execute('DELETE FROM task_idempotency WHERE task_id = ?', (task_id,))
            connection.execute('DELETE FROM resource_samples WHERE task_id = ?', (task_id,))
            connection.commit()
        # 快照整批送回收站；失败的路径原样留在磁盘，并如实回给界面。
        recycled, failed = trash.recycle([path for path in stale if path.is_file()])
        removed.extend(recycled)
        failed_snapshots.extend(failed)
        for path in recycled:
            # 任务自己的冻结目录（keyframe-tasks/<task_id>/）搬空后一并移除，
            # 否则会在工作区留下一个空目录。共享模板目录名不是任务 id，不受影响。
            holder = Path(path).parent
            if holder.name == task_id and holder.is_dir() and not any(holder.iterdir()):
                holder.rmdir()
        deleted.append(task_id)
        purged_assets.setdefault(project_id, set()).update({str(entry.get('asset_id') or '')})
    for project_id, ids in purged_assets.items():
        if project_id:
            library_archive.prune_assets(project_id, {item for item in ids if item})
    backend.TASK_SCHEDULER.wake()
    backend.task_plan_directory().joinpath(f'{request.plan_id}.json').unlink(missing_ok=True)
    return {'deleted': deleted, 'archived': archived, 'removed_snapshots': removed,
            'removed_outputs': removed_outputs,
            'failed': [*failed_outputs, *failed_snapshots],
            'recycled': True,
            'preserved': '模型文件、归档区与其它任务的产物未动；被清除的快照与产物已移入 Windows 回收站，可恢复。'}


@ROUTER.get('/api/generated-orphans')
def generated_orphans() -> dict[str, Any]:
    """只读：列出任务删除碰不到的生成物，供用户自行决定要不要清理。"""
    return orphan_scan.scan()
