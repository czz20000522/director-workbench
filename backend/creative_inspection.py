"""Read-only media facts and presentation over existing shot/assembly contracts."""
from functools import lru_cache
from pathlib import Path
import math


@lru_cache(maxsize=256)
def _probe(path, size, modified_ns):
    try:
        import av
        with av.open(path) as container:
            video = next(iter(container.streams.video), None)
            if video and Path(path).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
                return dict(known=True, width=video.width, height=video.height,
                            duration_seconds=None, fps=None, frames=None, audio_streams=0)
            audio = next(iter(container.streams.audio), None)
            stream = video or audio
            seconds = float(stream.duration * stream.time_base) if stream and stream.duration is not None and stream.time_base else (
                float(container.duration / av.time_base) if container.duration is not None else None)
            return dict(known=True, width=video.width if video else None, height=video.height if video else None,
                        duration_seconds=round(seconds, 6) if seconds and math.isfinite(seconds) and seconds > 0 else None,
                        fps=float(video.average_rate) if video and video.average_rate else None,
                        frames=video.frames if video and video.frames else None, audio_streams=len(container.streams.audio))
    except Exception:
        return dict(known=False, reason='metadata_unavailable', width=None, height=None,
                    duration_seconds=None, fps=None, frames=None, audio_streams=None)


def probe(path):
    """Caller must authorize the resolved file before probing or using cache."""
    try:
        stat = Path(path).stat()
        return dict(_probe(str(path), stat.st_size, stat.st_mtime_ns))
    except OSError:
        return dict(known=False, reason='media_unavailable', duration_seconds=None)


def comparison(reference, planned, *, audio_policy, first_frame=None):
    dimensions = ('width', 'height', 'duration_seconds', 'fps')
    differences = []
    source = (reference or {}).get('facts') or {}
    for field in dimensions:
        left, right = source.get(field), planned.get(field)
        if left is None or right is None:
            status = 'unknown'
        else:
            status = 'same' if abs(float(left) - float(right)) < .001 else 'different'
        differences.append(dict(field=field, reference=left, planned=right, status=status))
    dimensions_known = all(source.get(key) and planned.get(key) for key in ('width', 'height'))
    ratio = ('same' if abs(source['width'] / source['height'] - planned['width'] / planned['height']) < .001
             else 'different') if dimensions_known else 'unknown'
    first = first_frame or {}
    first_ratio_diff = bool(all(first.get(key) and planned.get(key) for key in ('width', 'height')) and
                            abs(first['width'] / first['height'] - planned['width'] / planned['height']) > .001)
    differences.append(dict(field='audio_strategy', reference='has_audio' if source.get('audio_streams') else
                            'silent' if source.get('audio_streams') == 0 else None,
                            planned=audio_policy, status='review_required'))
    return dict(reference=reference, planned=planned, differences=differences, aspect_ratio=ratio,
                first_frame=dict(facts=first, preprocessing='stretch' if planned.get('model') == 'H3' else 'unknown',
                                 aspect_mismatch=first_ratio_diff,
                                 evidence='MiniMaxH3ImageToVideo: _resize(first_frame, width, height, "disabled")'
                                 if planned.get('model') == 'H3' else None),
                blocking=False, note='规格差异供主动选择，不会自动修改方案或保证画质。')


def version_differences(current, previous):
    keys = ('prompt', 'duration_seconds', 'first_frame', 'last_frame', 'audio_guide', 'workflow', 'seed')
    def effective(snapshot, key):
        execution = snapshot.get('execution_snapshot') or {}
        parameters = execution.get('parameters') or {}
        if key == 'seed' and parameters.get('seed') is not None:
            return parameters['seed']
        if key == 'duration_seconds':
            for name in ('duration', 'duration_seconds'):
                if parameters.get(name) is not None:
                    return parameters[name]
        if key == 'workflow' and execution.get('source_workflow'):
            return execution['source_workflow']
        if key == 'prompt' and execution.get('prompt') is not None:
            return execution['prompt']
        return snapshot.get(key)
    return [dict(field=key, current=effective(current, key), previous=effective(previous, key)) for key in keys
            if effective(current, key) != effective(previous, key)]


def change_impact(document, segment_id):
    """Describe existing invalidation; dependencies are rechecks, not invented stales."""
    rows = [row for row in document.get('segments', []) if isinstance(row, dict)]
    downstream = [row['id'] for row in rows if segment_id in (row.get('dependencies') or [])]
    joins = [f"{left['id']}>{right['id']}" for left, right in zip(rows, rows[1:])
             if segment_id in (left.get('id'), right.get('id'))]
    return dict(revision=document.get('revision', 0), segment_id=segment_id,
                current_version_task_id=next((row.get('current_version_task_id') for row in rows if row.get('id') == segment_id), None),
                effects=[dict(kind='join_review', id=key, effect='stale_when_video_changes') for key in joins] +
                        [dict(kind='dependency_input', id=key, effect='needs_recheck',
                              note='下游已有冻结任务不会改写；重新提交时使用其保存输入，续接帧须主动重新绑定。') for key in downstream] +
                        ([dict(kind='assembly_candidate', id='MASTER', effect='stale_when_video_changes')]
                         if (document.get('assembly') or {}).get('output') else []),
                no_effect='预览或保存审核理由不切换视频；换当前视频或修改取片时长会改变接点签名。',
                old_candidates_preserved=True)
