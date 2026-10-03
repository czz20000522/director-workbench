"""Small atomic task receipts; no model work runs in request threads."""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SESSION = uuid.uuid4().hex
LOCK = threading.RLock()
ACTIVE = {'queued', 'running', 'needs_reconcile'}


def write(root: Path, job: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f'.{job["id"]}.{uuid.uuid4().hex}.tmp'
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(root / f'{job["id"]}.json')


def records(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in root.glob('*.json'):
        job = json.loads(path.read_text(encoding='utf-8'))
        if job.get('status') in ACTIVE and job.get('owner') != SESSION:
            job = {**job, 'status': 'needs_reconcile', 'error': '服务重启后原分析进程与结果尚未核对，不会自动重跑。'}
        result.append(job)
    return sorted(result, key=lambda job: job['created_at'], reverse=True)


def create(root: Path, project_id: str, analysis_id: str) -> dict[str, Any]:
    job = {'id': uuid.uuid4().hex, 'project_id': project_id, 'analysis_id': analysis_id,
           'owner': SESSION, 'status': 'queued', 'created_at': time.time(), 'updated_at': time.time(), 'error': None}
    write(root, job)
    return job


def update(root: Path, job: dict[str, Any], **fields: Any) -> dict[str, Any]:
    with LOCK:
        updated = {**job, **fields, 'updated_at': time.time()}
        write(root, updated)
        return updated


def public(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {key: value for key, value in job.items() if key != 'owner'}
