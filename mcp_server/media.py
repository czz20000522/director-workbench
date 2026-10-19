"""Remote-safe task, reference and media presentation for the MCP adapter.

The stdio adapter talks to the same business routes as the workbench UI, which
return server-owned paths for local rendering.  Remote MCP callers must not see
those paths: this module redacts machine paths, turns candidate media into
stable ``media_ref`` values plus short-lived signed URLs, and keeps the
project/task/version/asset identity needed for later explicit actions.
"""
from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

INLINE_MEDIA_LIMIT = 1024 * 1024
MAX_INLINE_MEDIA_LIMIT = 4 * 1024 * 1024
MEDIA_REF_CHUNK = 100

# Machine paths that must never appear in a remote response.  Controlled URLs
# start with ``/api/`` and are deliberately not matched.  The pattern also
# catches paths embedded in error text.
_SERVER_PATH = re.compile(
    r'(?i)(?:'
    r'[a-z]:[\\/][^\s"\'<>]*'
    r'|\\\\[^\s"\'<>]+'
    r'|(?:ComfyUI-Workspace|ComfyUI-Shared|ComfyUI|导演工作台工作目录|reference-analysis|'
    r'workflows[\\/]snapshots|assets[\\/]uploads|director\.project\.json)(?:[\\/][^\s"\'<>]*)+'
    r'|(?<![\w/.:-])(?:output|input|plans|assets|series|workflows)/[^\s"\'<>]*'
    r')'
)

_TASK_KEYS = ('id', 'asset_id', 'status', 'project_id', 'batch_id', 'sequence', 'prompt_id',
              'created_at', 'updated_at', 'error', 'stop_requested', 'queue')
_TASK_PAYLOAD_KEYS = ('kind', 'operation', 'asset_id', 'project_id', 'pipeline_stage_id', 'batch_id',
                      'submission_receipt', 'parameters', 'source_asset', 'background_asset',
                      'output_size', 'request')
_TASK_RESULT_PATH_KEYS = {'output', 'outputs', 'native_output', 'native_outputs',
                          'published_outputs', 'published_native_outputs', 'published_native_output'}
_JOB_KEYS = ('id', 'status', 'task_id', 'error', 'created_at', 'updated_at', 'progress', 'queue',
             'source_revision', 'recovered_at')


def redact_paths(value: Any) -> Any:
    """Recursively replace machine-path-like strings with a redaction marker."""
    if isinstance(value, str):
        return _SERVER_PATH.sub('[server-path]', value)
    if isinstance(value, list):
        return [redact_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_paths(item) for key, item in value.items()}
    return value


def is_server_path(value: Any) -> bool:
    return isinstance(value, str) and bool(_SERVER_PATH.search(value))


async def issue_media(client: Any, project_id: str, refs: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve stable refs to short-lived URLs; unavailable entries are skipped."""
    unique = [ref for ref in dict.fromkeys(refs) if isinstance(ref, str) and ref]
    issued: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique), MEDIA_REF_CHUNK):
        chunk = unique[start:start + MEDIA_REF_CHUNK]
        result = await client.request('POST', f'/api/projects/{quote(project_id, safe="")}/media-refs',
                                      json={'media_refs': chunk})
        if not result.get('ok'):
            continue
        for item in (result.get('data') or {}).get('refs', []):
            if isinstance(item, dict) and item.get('ok') and item.get('media_ref'):
                issued[item['media_ref']] = item
    return issued


def _media_entries(refs: list[str], issued: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [issued[ref] for ref in refs if ref in issued]


async def shape_task(client: Any, project_id: str, task: dict[str, Any]) -> dict[str, Any]:
    """Return a receipt with identity/status facts and controlled media only."""
    if not isinstance(task, dict):
        return task
    value = {key: task.get(key) for key in _TASK_KEYS if key in task}
    payload = task.get('payload') if isinstance(task.get('payload'), dict) else {}
    value['payload'] = redact_paths({key: payload.get(key) for key in _TASK_PAYLOAD_KEYS if key in payload})
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    value['result'] = redact_paths({key: item for key, item in result.items() if key not in _TASK_RESULT_PATH_KEYS})
    if task.get('status') == 'succeeded' and task.get('id'):
        refs = task_media_refs(task)
        issued = await issue_media(client, project_id, refs)
        media = _media_entries(refs, issued)
        if media:
            value['media'] = media
            video = next((item for item in media if item.get('kind') == 'video'), media[0])
            value['candidate_ref'] = video['media_ref']
    return value


def task_media_refs(task: dict[str, Any]) -> list[str]:
    task_id = task.get('id')
    if not task_id:
        return []
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    refs: list[str] = []
    if result.get('published_outputs') or result.get('output') or result.get('outputs'):
        refs.append(f'task:{task_id}:video:0')
    for field in ('published_outputs', 'outputs', 'native_outputs', 'published_native_outputs'):
        values = result.get(field)
        if isinstance(values, list) and values:
            refs.extend(f'task:{task_id}:{field}:{index}' for index in range(len(values)))
    if result.get('output') is not None:
        refs.append(f'task:{task_id}:output:0')
    if not refs:
        refs.append(f'task:{task_id}:video:0')
    return list(dict.fromkeys(refs))


async def shape_tasks(client: Any, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    tasks = data.get('tasks') if isinstance(data.get('tasks'), list) else []
    shaped = [await shape_task(client, project_id, task) for task in tasks if isinstance(task, dict)]
    return {**data, 'tasks': shaped}


async def shape_receipt(client: Any, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return await shape_task(client, project_id, data) if isinstance(data, dict) else data


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    return redact_paths({key: job.get(key) for key in _JOB_KEYS if key in job})


# ------------------------------------------------------------------ references

def _observation_refs(analysis_id: str, observation: dict[str, Any]) -> list[str]:
    observation_id = observation.get('id')
    if not observation_id:
        return []
    refs = []
    for slot, key in (('segment', 'segment_path'), ('frame', 'frame_path'),
                      ('audio', 'audio_path'), ('transcript', 'transcript_path')):
        if observation.get(key):
            refs.append(f'reference:{analysis_id}:{observation_id}:{slot}')
    for index, frame in enumerate(observation.get('frames') or []):
        if isinstance(frame, dict) and frame.get('path'):
            refs.append(f'reference:{analysis_id}:{observation_id}:frame_{index}')
    return refs


def _clean_reference_observation(analysis_id: str, observation: dict[str, Any],
                                 media_by_ref: dict[str, dict[str, Any]],
                                 refs: list[str]) -> dict[str, Any]:
    value = {key: item for key, item in observation.items()
             if key not in {'segment_path', 'frame_path', 'audio_path', 'transcript_path'}}
    frames = []
    for index, frame in enumerate(observation.get('frames') or []):
        if not isinstance(frame, dict):
            continue
        ref = f'reference:{analysis_id}:{observation.get("id")}:frame_{index}'
        frames.append({'time_seconds': frame.get('time_seconds'),
                       **({'media': media_by_ref[ref]} if ref in media_by_ref else {})})
    value['frames'] = frames
    media = _media_entries(refs, media_by_ref)
    if media:
        value['media'] = media
    return value


async def shape_observation(client: Any, project_id: str, analysis_id: str,
                            observation: dict[str, Any]) -> dict[str, Any]:
    refs = _observation_refs(analysis_id, observation)
    issued = await issue_media(client, project_id, refs)
    return redact_paths(_clean_reference_observation(analysis_id, observation, issued, refs))


async def shape_analysis(client: Any, project_id: str, document: dict[str, Any]) -> dict[str, Any]:
    """Return a path-free analysis while keeping observation/artifact identity."""
    if not isinstance(document, dict):
        return document
    analysis_id = str(document.get('id') or '')
    refs: list[str] = []
    source = document.get('source') if isinstance(document.get('source'), dict) else {}
    source_ref = f'reference-source:{analysis_id}' if source.get('path') else None
    if source_ref:
        refs.append(source_ref)
    observation_refs: dict[str, list[str]] = {}
    for observation in document.get('observations') or []:
        if not isinstance(observation, dict):
            continue
        current = _observation_refs(analysis_id, observation)
        observation_refs[str(observation.get('id'))] = current
        refs.extend(current)
    timeline_refs: dict[str, list[str]] = {}
    observation_ids = set(observation_refs)
    for segment in document.get('timeline') or []:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get('id') or '')
        if segment_id in observation_ids:
            timeline_refs[segment_id] = observation_refs[segment_id]
            continue
        current = []
        if segment.get('frame') or segment.get('frame_path'):
            current.append(f'reference-timeline:{analysis_id}:{segment_id}:frame')
        for index, frame in enumerate(segment.get('frames') or []):
            if isinstance(frame, dict) and frame.get('path'):
                current.append(f'reference-timeline:{analysis_id}:{segment_id}:frame_{index}')
        timeline_refs[segment_id] = current
        refs.extend(current)
    artifact_refs: dict[tuple[str, int], str] = {}
    artifacts = document.get('artifacts') if isinstance(document.get('artifacts'), dict) else {}
    for artifact_id, artifact in artifacts.items():
        if not isinstance(artifact, dict) or not isinstance(artifact.get('evidence'), list):
            continue
        for index, evidence in enumerate(artifact['evidence']):
            if is_server_path(evidence):
                artifact_refs[(str(artifact_id), index)] = f'reference-artifact:{analysis_id}:{artifact_id}:{index}'
    refs.extend(artifact_refs.values())
    issued = await issue_media(client, project_id, refs)
    shaped = {key: item for key, item in document.items()
              if key not in {'production_seed', 'semantic_result', 'source'}}
    shaped['production_seed_available'] = bool(document.get('production_seed'))
    source = {key: item for key, item in source.items() if key != 'path'}
    if source_ref and source_ref in issued:
        source['media'] = issued[source_ref]
    shaped['source'] = source
    shaped['observations'] = [
        _clean_reference_observation(analysis_id, observation, issued, observation_refs.get(str(observation.get('id')), []))
        for observation in document.get('observations') or [] if isinstance(observation, dict)
    ]
    timeline = []
    for segment in document.get('timeline') or []:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get('id') or '')
        value = {key: item for key, item in segment.items() if key not in {'frame', 'frame_path'}}
        frames = []
        for index, frame in enumerate(segment.get('frames') or []):
            if not isinstance(frame, dict):
                continue
            ref = f'reference-timeline:{analysis_id}:{segment_id}:frame_{index}'
            frames.append({'time_seconds': frame.get('time_seconds'),
                           **({'media': issued[ref]} if ref in issued else {})})
        if frames:
            value['frames'] = frames
        media = _media_entries(timeline_refs.get(segment_id, []), issued)
        if media:
            value['media'] = media
        timeline.append(value)
    shaped['timeline'] = timeline
    shaped_artifacts: dict[str, Any] = {}
    for artifact_id, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            shaped_artifacts[artifact_id] = redact_paths(artifact)
            continue
        value = dict(artifact)
        evidence = artifact.get('evidence') if isinstance(artifact.get('evidence'), list) else []
        value['evidence'] = [item for index, item in enumerate(evidence)
                             if (str(artifact_id), index) not in artifact_refs]
        value['evidence_media'] = [issued[ref] for (key, index), ref in artifact_refs.items()
                                   if key == str(artifact_id) and ref in issued]
        if not value['evidence_media']:
            value.pop('evidence_media')
        shaped_artifacts[str(artifact_id)] = redact_paths(value)
    shaped['artifacts'] = shaped_artifacts
    return redact_paths(shaped)


async def shape_versions(client: Any, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    items = data.get('items') if isinstance(data.get('items'), list) else []
    refs = [f"task:{item['task_id']}:video:0" for item in items
            if isinstance(item, dict) and item.get('task_id') and item.get('video_path')]
    issued = await issue_media(client, project_id, refs)
    shaped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = redact_paths({key: entry for key, entry in item.items() if key != 'video_path'})
        ref = f"task:{item.get('task_id')}:video:0"
        if ref in issued:
            value['media'] = [issued[ref]]
            value['candidate_ref'] = issued[ref]['media_ref']
        shaped.append(value)
    return {**redact_paths({key: entry for key, entry in data.items() if key != 'items'}), 'items': shaped}


async def inline_media(client: Any, project_id: str, media_ref: str,
                       max_bytes: int = INLINE_MEDIA_LIMIT) -> dict[str, Any]:
    """Issue a URL and inline the bytes only when they fit the caller's budget."""
    if max_bytes > MAX_INLINE_MEDIA_LIMIT:
        raise ValueError(f'内联媒体最多 {MAX_INLINE_MEDIA_LIMIT // (1024 ** 2)} MiB，大文件请使用受控 URL')
    issued = await issue_media(client, project_id, [media_ref])
    entry = issued.get(media_ref)
    if entry is None:
        return {'ok': False, 'error': {'code': 'media_ref_unavailable',
                                       'message': '媒体引用不存在、无权访问或文件已变化', 'retryable': False}}
    size = entry.get('size_bytes')
    response: dict[str, Any] = {'media_ref': entry['media_ref'], 'url': entry['url'],
                                'expires_at': entry['expires_at'], 'kind': entry.get('kind'),
                                'filename': entry.get('filename'), 'size_bytes': size,
                                'content_type': entry.get('content_type'), 'inline': False}
    if not isinstance(size, int) or size > max_bytes:
        response['inline_reason'] = 'file_exceeds_inline_budget'
        return response
    fetched = await client.fetch(entry['url'])
    if not fetched.get('ok'):
        return fetched
    content = fetched['data']['content']
    response.update(inline=True, content_base64=base64_encode(content),
                    returned_bytes=len(content), content_type=fetched['data'].get('content_type') or response['content_type'])
    return response


def base64_encode(content: bytes) -> str:
    return base64.b64encode(content).decode('ascii')


def analysis_identity(document: dict[str, Any]) -> dict[str, Any]:
    return {'id': document.get('id'), 'project_id': document.get('project_id'),
            'revision': document.get('revision'), 'status': document.get('status')}
