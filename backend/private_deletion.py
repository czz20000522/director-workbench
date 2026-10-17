"""Two-step, account-scoped filesystem deletion via the installed safe-delete skill."""
import json
import re
import subprocess
import sys
from pathlib import Path

from fastapi import HTTPException


def _script():
    path = Path.home() / '.agents/skills/safe-delete/scripts/safe_delete.py'
    if not path.is_file():
        raise HTTPException(503, '本机缺少 safe-delete 工具，暂不能删除磁盘目录')
    return path


def _run(*arguments):
    result = subprocess.run([sys.executable, str(_script()), *map(str, arguments)],
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise HTTPException(409, (result.stderr or '删除校验失败').strip()[:500])
    return json.loads(result.stdout)


def plan(root: Path, target: Path, manifest_dir: Path):
    manifest_dir.mkdir(parents=True, exist_ok=True)
    result = _run('plan', '--root', root, '--target', target,
                  '--manifest-dir', manifest_dir, '--ttl-minutes', '20', '--sample-size', '10')
    return {'plan_id': result['plan_id'], 'target': str(target),
            'snapshot': result['snapshot'], 'sample': result['sample'],
            'expires_at': result['expires_at']}


def apply(root: Path, target: Path, manifest_dir: Path, plan_id: str):
    if not re.fullmatch(r'[0-9a-f]{24}', plan_id):
        raise HTTPException(422, '删除计划编号无效')
    manifest = manifest_dir / f'{plan_id}.json'
    details = _run('inspect', '--manifest', manifest)
    if details.get('roots') != [str(root)] or details.get('targets') != [str(target)]:
        raise HTTPException(409, '删除范围与预览时不一致，请重新预览')
    result = _run('apply', '--manifest', manifest)
    return {'deleted': result.get('deleted_targets', []), 'snapshot': details['snapshot']}
