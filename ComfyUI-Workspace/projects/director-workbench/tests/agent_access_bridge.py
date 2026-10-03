"""Actual isolated HTTP handlers for the complete App browser; no socket or GPU."""
import base64
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from backend import app as backend
from test_private_workbench import private_workbench
from test_automatic_creation import seed_test_models, complete_frozen_task

original_root = backend.ROOT
root = Path(tempfile.mkdtemp(prefix='dw026-agent-browser-', dir=backend.PROJECT.parents[1] / 'runtime'))
patch = pytest.MonkeyPatch()
agent, other, ownership, _ = private_workbench.__wrapped__(root, patch)
backend.app.user_middleware[0].kwargs['origins'] = ('http://testserver', 'http://localhost:4196')
backend.app.middleware_stack = None
browser = TestClient(backend.app)
seed_test_models(root)
patch.setattr(backend, 'queue_task', lambda *args: None)
patch.setattr(backend, 'require_submission_capacity', lambda: {})
def fake_comfy(path, payload=None, **kwargs):
    if path == '/queue': return {'queue_running': [], 'queue_pending': []}
    if path == '/system_stats': return {'system': {}, 'devices': []}
    if path.startswith('/history'): return {}
    raise AssertionError('No real engine access: ' + path)
patch.setattr(backend, 'request_json', fake_comfy)
initial = agent.post('/api/projects/create', json={'series': 'Isolated', 'title': 'Browser starting project', 'select': True}).json()['project']
video = root / 'synthetic-video.mp4'
ffmpeg = original_root / 'ComfyUI-Shared/tools/index-tts/ffmpeg.exe'
subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=24', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True, capture_output=True)
print(json.dumps({'ready': True, 'project_id': initial['id'], 'root': str(root)}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    try:
        if request.get('operation') == 'complete':
            task = agent.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
            complete_frozen_task(task, root)
            done = agent.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
            Path(done['result']['published_outputs'][0]).write_bytes(video.read_bytes())
            print(json.dumps({'status': 200, 'data': done}), flush=True)
            continue
        if request.get('operation') == 'historical':
            # Fixture-only alternate result identity; retain the sole actual task as history.
            task = agent.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
            _, plan_path = backend.load_project_plan(task['project_id'])
            backend.record_segment_result(task['asset_id'], 'isolated-current-receipt', task['payload']['workflow'],
                                          task['result'], plan_path, task_id='isolated-current-receipt', payload=task['payload'])
            print(json.dumps({'status': 200, 'data': {'fixture_current': 'isolated-current-receipt'}}), flush=True)
            continue
        client = agent if request.get('channel') == 'agent' else browser
        headers = request.get('headers', {})
        if client is browser:
            # Require the real routed browser cookie/Origin/user header, never fixture Bearer fallback.
            browser.cookies.clear()
            assert not headers.get('authorization')
        response = client.request(request['method'], request['path'], headers=headers,
                                  **({'json': request['body']} if 'body' in request else {}))
        response_headers = {key: value for key, value in response.headers.items() if key.lower() in {'content-type', 'set-cookie', 'content-range', 'accept-ranges'}}
        try:
            data = response.json()
            result = {'status': response.status_code, 'data': data, 'headers': response_headers}
        except ValueError:
            result = {'status': response.status_code, 'binary': base64.b64encode(response.content).decode(), 'headers': response_headers}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({'status': 500, 'data': {'detail': type(error).__name__ + ': ' + str(error), 'traceback': traceback.format_exc()}}), flush=True)
