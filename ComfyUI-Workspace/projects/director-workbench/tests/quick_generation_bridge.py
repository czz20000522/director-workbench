"""Real authenticated HTTP handlers with isolated storage and fake GPU dispatch."""
import base64
import json
import logging
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import sys
from pathlib import Path

import pytest
import av
import numpy as np
from fastapi.testclient import TestClient

from backend import app as backend
from test_enhancement import nodes as enhancement_nodes
from test_private_workbench import private_workbench
from test_creation_assistant import assistant
from test_automatic_creation import complete_frozen_task
from test_keyframe_inputs import reference_image

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=False)
patch = pytest.MonkeyPatch()
clients = private_workbench.__wrapped__(root, patch)
owner, other, queued = assistant.__wrapped__(clients, patch)
builder = backend.PROJECT / 'tools' / 'build_assembly_workflow.py'
builder.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(Path(__file__).resolve().parents[1] / 'tools' / builder.name, builder)
patch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
patch.setattr(backend.keyframes, 'capability', lambda *args, **kwargs: {'available': True})
engine = backend.request_json
patch.setattr(backend, 'request_json', lambda path, *args, **kwargs: enhancement_nodes(True) if path == '/object_info' else engine(path, *args, **kwargs))
logging.getLogger('httpx').setLevel(logging.WARNING)
backend.app.user_middleware[0].kwargs['origins'] = ('http://testserver', 'http://localhost:4197')
backend.app.middleware_stack = None
browser = TestClient(backend.app)

# Chromium's native download bypasses Playwright route interception. Serve
# these two existing authenticated handlers on the same isolated test origin;
# no fixture identity is injected and no production endpoint is contacted.
class NativeDownloadHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # DevTools omits uploaded file bytes from intercepted multipart bodies.
        # Let the real browser send them to this isolated authenticated handler.
        path = urlsplit(self.path).path
        if not path.startswith('/api/projects/') or not path.endswith('/upload'):
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        response = TestClient(backend.app).post(self.path, headers=dict(self.headers), content=body)
        self.send_response(response.status_code)
        self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
        self.send_header('Content-Length', str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def do_GET(self):
        if urlsplit(self.path).path not in {'/media-file', '/material-file'}:
            self.send_error(404)
            return
        response = TestClient(backend.app).get(self.path, headers=dict(self.headers))
        self.send_response(response.status_code)
        for key in ('content-type', 'content-range', 'accept-ranges'):
            if key in response.headers:
                self.send_header(key, response.headers[key])
        self.send_header('Content-Length', str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def log_message(self, *args):
        pass

native_downloads = ThreadingHTTPServer(('127.0.0.1', 4197), NativeDownloadHandler)
threading.Thread(target=native_downloads.serve_forever, daemon=True).start()
picture = reference_image(root)
def synthetic_video(path, seconds):
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=24)
        stream.width, stream.height, stream.pix_fmt = 64, 64, 'yuv420p'
        for _ in range(round(seconds * 24)):
            frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), 96, dtype=np.uint8), format='rgb24')
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
video = root / 'synthetic.mp4'
synthetic_video(video, 5)
print(json.dumps({'ready': True, 'picture': str(picture)}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get('operation') == 'complete':
        task = owner.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
        if task['payload'].get('pipeline_stage_id') == 'media-enhancement':
            # Explicit CPU executor fixture; the real scheduler callback,
            # publisher and enhancement output validation still run.
            recipe = task['payload']['enhancement']
            target = backend.OUTPUT_ROOT / '导演工作台工作目录' / task['project_id'] / f"{task['id']}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(target), 'w') as media:
                stream = media.add_stream('png', rate=1)
                stream.width = recipe['source_facts']['width'] * 2
                stream.height = recipe['source_facts']['height'] * 2
                stream.pix_fmt = 'rgb24'
                frame = av.VideoFrame.from_ndarray(np.full((stream.height, stream.width, 3), 96, dtype=np.uint8), format='rgb24')
                for packet in stream.encode(frame): media.mux(packet)
                for packet in stream.encode(): media.mux(packet)
            def fake_engine(path, data=None):
                if path == '/prompt': return {'prompt_id': task['id']}
                if path.startswith('/history/'):
                    return {task['id']: {'status': {'status_str': 'success'}, 'outputs': {'save': {'images': [
                        {'filename': target.name, 'subfolder': str(target.parent.relative_to(backend.OUTPUT_ROOT)), 'type': 'output'}]}}}}
                return {'queue_running': [], 'queue_pending': []}
            with pytest.MonkeyPatch.context() as execution:
                execution.setattr(backend, 'request_json', fake_engine)
                execution.setattr(backend, 'sample_resources', lambda _: None)
                backend.run_workflow_submission(task['id'], task['payload'])
        elif task['payload'].get('assembly_asset_id') == task['asset_id']:
            # Executor result fixture only: the real builder/API/recorders run,
            # while ComfyUI graph execution remains explicitly stubbed.
            plan = owner.get(f"/api/projects/{task['project_id']}/plan").json()
            target = backend.OUTPUT_ROOT / f"{task['id']}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            synthetic_video(target, sum(segment['duration_seconds'] for segment in plan['segments']))
            published = backend.resolve_registered_path(backend.require_project_manifest(task['project_id'])['workspace_root']) / 'assets' / target.name
            shutil.copy2(target, published)
            result = {'outputs': [target.name], 'published_outputs': [str(published)]}
            backend.set_task(task['id'], status='succeeded', result=json.dumps(result), prompt_id=task['id'])
            backend.record_assembly_result(task['id'], task['payload']['workflow'], result, Path(task['payload']['plan_path']))
        elif task['payload'].get('kind') == 'keyframe':
            backend.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            image = backend.OUTPUT_ROOT / '导演工作台工作目录' / task['project_id'] / f"{task['id']}.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(picture.read_bytes())
            backend.complete_keyframe_task(task, {'status': 'success', 'output': str(image), 'width': 32, 'height': 48})
        else:
            complete_frozen_task(task, root)
            done = owner.get(f"/api/projects/{request['project_id']}/tasks/{request['task_id']}").json()
            Path(done['result']['published_outputs'][0]).write_bytes(video.read_bytes())
        print(json.dumps({'status': 200}), flush=True)
        continue
    if request.get('operation') == 'delivery-proof':
        pid = request['project_id']
        task = owner.get(f"/api/projects/{pid}/tasks/{request['task_id']}").json()
        plan = owner.get(f'/api/projects/{pid}/plan').json()
        output_path = plan['assembly']['output']
        staged = task['payload']['execution_snapshot']['staged_materials']
        print(json.dumps({'status': 200, 'graph': json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8')),
                          'staged': [{**item, 'copy_matches_source': Path(item['path']).read_bytes() == Path(item['source']).read_bytes()} for item in staged],
                          'plan': plan, 'foreign_task_status': other.get(f"/api/projects/{pid}/tasks/{task['id']}").status_code,
                          'foreign_file_status': other.get('/media-file', params={'path': output_path}).status_code}), flush=True)
        continue
    if request.get('operation') == 'stats':
        print(json.dumps({'status': 200, 'queued': queued,
                          'other_projects': other.get('/api/projects').json()['projects']}), flush=True)
        continue
    headers = request.get('headers', {})
    assert not headers.get('authorization'), 'Use routed login cookie, never fixture identity'
    browser.cookies.clear()
    kwargs = {'headers': headers}
    if 'body' in request:
        kwargs['content'] = base64.b64decode(request['body'])
    response = browser.request(request['method'], request['path'], **kwargs)
    response_headers = {k: v for k, v in response.headers.items() if k.lower() in
                        {'content-type', 'set-cookie', 'content-range', 'accept-ranges'}}
    try:
        result = {'status': response.status_code, 'data': response.json(), 'headers': response_headers}
    except ValueError:
        result = {'status': response.status_code, 'binary': base64.b64encode(response.content).decode(), 'headers': response_headers}
    print(json.dumps(result, ensure_ascii=False), flush=True)
