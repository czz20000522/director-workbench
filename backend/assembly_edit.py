"""Version-bound join reviews for a project's no-GPU assembly plan."""
from __future__ import annotations

from typing import Any


def join_key(left_id: str, right_id: str) -> str:
    return f'{left_id}>{right_id}'


def signature(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        'left_video': (left.get('video') or {}).get('path'),
        'right_video': (right.get('video') or {}).get('path'),
        'left_version': left.get('current_version_task_id'),
        'right_version': right.get('current_version_task_id'),
        'left_duration': left.get('duration_seconds'),
        'right_duration': right.get('duration_seconds'),
    }


def joins(document: dict[str, Any], *, missing_videos=()) -> list[dict[str, Any]]:
    segments = [item for item in document.get('segments', []) if isinstance(item, dict)]
    saved = ((document.get('assembly_edit') or {}).get('joins') or {})
    missing = set(missing_videos)
    result = []
    for left, right in zip(segments, segments[1:]):
        key = join_key(str(left.get('id')), str(right.get('id')))
        record = saved.get(key) if isinstance(saved, dict) else None
        current = signature(left, right)
        stale = bool(record and record.get('signature') != current)
        status = 'stale' if stale else (record or {}).get('status', 'pending')
        review_ready = bool(current['left_video'] and current['right_video'] and
                            left.get('id') not in missing and right.get('id') not in missing)
        result.append({
            'key': key, 'left_id': left.get('id'), 'right_id': right.get('id'),
            'cut_seconds': left.get('end_seconds'), 'signature': current,
            'left_preview_start': max(0, float(left.get('duration_seconds') or 0) - 1.5),
            'right_preview_end': min(1.5, float(right.get('duration_seconds') or 0)),
            'status': status,
            'review_ready': review_ready,
            'review_completed': review_ready and status == 'approved',
            'constraint': (record or {}).get('constraint', ''),
            'note': (record or {}).get('note', ''),
        })
    return result


def review_readiness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Explicit current-version review, separate from legacy assembly eligibility."""
    remaining = [row['key'] for row in rows if not row['review_completed']]
    state = 'not_applicable' if not rows else 'approved'
    for status in ('video_required', 'stale', 'redo_left', 'redo_right', 'pending'):
        if any(('video_required' if not row['review_ready'] else row['status']) == status for row in rows):
            state = status
            break
    if remaining and state == 'approved':
        state = 'pending'  # Unknown persisted review states cannot grant completion.
    return {'applicable': bool(rows), 'completed': bool(rows) and not remaining,
            'state': state, 'remaining_joins': remaining}


def blocking_joins(document: dict[str, Any]) -> list[str]:
    # Existing projects without an edit plan retain their established behavior.
    if not isinstance(document.get('assembly_edit'), dict):
        return []
    return [item['key'] for item in joins(document) if not item['review_completed']]
