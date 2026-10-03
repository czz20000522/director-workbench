"""Isolated ASGI request bridge for a browser; no socket service or GPU dispatch."""
import json
import sys
import tempfile
from pathlib import Path

import pytest
from backend import app as backend
from test_private_workbench import private_workbench
from test_automatic_creation import seed_test_models, complete_frozen_task

root = Path(tempfile.mkdtemp(prefix='dw2324-browser-', dir=backend.PROJECT.parents[1] / 'runtime'))
patch = pytest.MonkeyPatch()
a, b, ownership, _ = private_workbench.__wrapped__(root, patch)
seed_test_models(root)
patch.setattr(backend, 'queue_task', lambda *args: None)
patch.setattr(backend, 'require_submission_capacity', lambda: {})
def fake_comfy(path, payload=None, **kwargs):
    if path == '/queue': return {'queue_running': [], 'queue_pending': []}
    if path == '/system_stats': return {'system': {}, 'devices': []}
    if path.startswith('/history'): return {}
    raise ValueError('Isolated engine unavailable; no real Comfy request: ' + path)
patch.setattr(backend, 'request_json', fake_comfy)
project = a.post('/api/projects/create', json={'series':'Isolated','title':'Automatic browser'}).json()['project']
print(json.dumps({'project_id':project['id'], 'root':str(root)}), flush=True)
for line in sys.stdin:
    request=json.loads(line)
    if request.get('operation') == 'complete':
        task=a.get(f"/api/projects/{project['id']}/tasks").json()['tasks'][0]
        complete_frozen_task(task, root)
        print(json.dumps({'status':200,'data':{'task_id':task['id']}}),flush=True)
        continue
    if request.get('operation') == 'state':
        response=a.get(f"/api/projects/{project['id']}/state")
    else:
        method=request['method']
        response=a.request(method, request['path'], **({'json':request['body']} if 'body' in request else {}))
    try: value=response.json()
    except ValueError: value={'detail':response.text}
    print(json.dumps({'status':response.status_code,'data':value},ensure_ascii=False),flush=True)
