"""Complete authenticated App fixture; only CPU synthetic video and fake provider."""
import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend
from test_creation_assistant import assistant
from test_private_workbench import private_workbench
from test_automatic_creation import complete_frozen_task

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
patch = pytest.MonkeyPatch()
clients = private_workbench.__wrapped__(root, patch)
agent, other, queued = assistant.__wrapped__(clients, patch)
backend.app.user_middleware[0].kwargs['origins'] = ('http://testserver', 'http://localhost:4197')
backend.app.middleware_stack = None
browser = TestClient(backend.app)
video = root / 'synthetic.mp4'
ffmpeg = sys.argv[2] if len(sys.argv) > 2 else shutil.which('ffmpeg')
assert ffmpeg, 'Pass a local ffmpeg executable for the CPU video fixture'
subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                'color=c=blue:s=64x64:r=24', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
               capture_output=True, check=True, timeout=30)
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get('operation') == 'stats':
        provider = next(iter(backend.ASSISTANT_SERVICES.values())).provider
        print(json.dumps({'status': 200, 'data': {'provider_calls': len(provider.calls), 'queued': len(queued)}}), flush=True)
        continue
    if request.get('operation') in ('hold_provider', 'release_provider'):
        agent.get('/api/assistant/sessions')
        provider = next(iter(backend.ASSISTANT_SERVICES.values())).provider
        if request['operation'] == 'hold_provider': provider.release.clear()
        else: provider.release.set()
        print(json.dumps({'status': 200, 'data': {'controlled': True}}), flush=True)
        continue
    if request.get('operation') == 'complete':
        task = agent.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
        complete_frozen_task(task, root)
        done = agent.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
        Path(done['result']['published_outputs'][0]).write_bytes(video.read_bytes())
        print(json.dumps({'status': 200, 'data': done}), flush=True)
        continue
    headers = request.get('headers', {})
    browser.cookies.clear()
    assert not headers.get('authorization'), 'Browser must use actual routed cookie, never fixture Bearer'
    response = browser.request(request['method'], request['path'], headers=headers,
                               **({'json': request['body']} if 'body' in request else {}))
    response_headers = {k: v for k, v in response.headers.items() if k.lower() in
                        {'content-type', 'set-cookie', 'content-range', 'accept-ranges'}}
    try: result = {'status': response.status_code, 'data': response.json(), 'headers': response_headers}
    except ValueError: result = {'status': response.status_code, 'binary': base64.b64encode(response.content).decode(), 'headers': response_headers}
    print(json.dumps(result, ensure_ascii=False), flush=True)
