"""Crash real workbench processes against SQLite and a CPU-only HTTP engine.

The engine accepts actual /prompt requests and survives the workbench crash.
Its controlled synthetic receipts do not certify a production ComfyUI restart.
"""
import base64
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PROJECT = Path(__file__).resolve().parents[1]
REPLY = 'restart-test: '


def workbench_process(root, engine_url):
    """Keep real lifecycle, routes, workers and urllib; isolate only storage/config."""
    sys.path.insert(0, str(PROJECT))
    from fastapi.testclient import TestClient
    from backend import app as backend
    from backend.private_mode import install
    from backend.private_sessions import AccountStore
    from backend.user_context import Layout

    project = root / 'p'
    catalog = project / 'projects'
    catalog.mkdir(parents=True, exist_ok=True)
    catalog_path = catalog / 'catalog.json'
    if not catalog_path.exists():
        catalog_path.write_text('{"projects": []}', encoding='utf-8')
    for name, value in {
        'ROOT': root, 'PROJECT': project, 'PROJECTS_ROOT': catalog,
        'PROJECT_CATALOG_PATH': catalog_path, 'DIRECTOR_WORKSPACES_ROOT': project / 'workspaces',
        'DB_PATH': root / 'tasks.sqlite3', 'RUNTIME': root / 'r',
        'INPUT_ROOT': root / 'input', 'OUTPUT_ROOT': root / 'output', 'COMFY_URL': engine_url,
    }.items():
        setattr(backend, name, value)
    backend.INPUT_ROOT.mkdir(exist_ok=True)
    backend.OUTPUT_ROOT.mkdir(exist_ok=True)
    accounts = AccountStore(root / 'accounts.json', permitted_root=root)
    if not accounts.path.exists():
        for owner in ('user001', 'user002'):
            accounts.create_account(owner, 'isolated-test')
    sessions, _ = install(backend, accounts=accounts,
        layout=Layout(root / 'ComfyUI-Workspace' / 'u', permitted_roots=(root,)),
        origins=('http://testserver',), administrators=())
    # Re-login after each process restart; raw sessions never leave this worker.
    headers = {owner: {'Authorization': 'Bearer ' + sessions.login(owner, 'isolated-test')}
               for owner in ('user001', 'user002')}
    with TestClient(backend.app) as client:
        print(REPLY + json.dumps({'ready': True, 'pid': os.getpid()}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            kwargs = {'headers': headers[command['owner']]}
            for key in ('json', 'params'):
                if key in command:
                    kwargs[key] = command[key]
            if 'upload' in command:
                kwargs['files'] = [('files', ('first.png', base64.b64decode(command['upload']), 'image/png'))]
            response = client.request(command['method'], command['path'], **kwargs)
            result = {'status': response.status_code}
            if response.headers.get('content-type', '').startswith('application/json'):
                result['body'] = response.json()
            else:
                result['bytes'] = base64.b64encode(response.content).decode('ascii')
            print(REPLY + json.dumps(result, ensure_ascii=False), flush=True)


class WorkbenchProcess:
    def __init__(self, root, engine_url):
        env = dict(os.environ, PYTHONUTF8='1', DIRECTOR_GPU_IDLE_GRACE_SECONDS='3600')
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker', str(root), engine_url],
            cwd=PROJECT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.replies = queue.Queue()
        self.noise = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.pid = self.receive()['pid']
        except BaseException:
            self.crash()
            raise

    def _read(self):
        for line in self.process.stdout:
            if line.startswith(REPLY):
                self.replies.put(json.loads(line[len(REPLY):]))
            else:
                self.noise.append(line.rstrip())
        self.replies.put(None)

    def receive(self):
        try:
            reply = self.replies.get(timeout=20)
        except queue.Empty:
            pytest.fail('Isolated workbench did not respond: ' + '\n'.join(self.noise[-20:]))
        assert reply is not None, '\n'.join(self.noise[-20:])
        return reply

    def request(self, owner, method, path, **kwargs):
        self.process.stdin.write(json.dumps({'owner': owner, 'method': method, 'path': path, **kwargs}) + '\n')
        self.process.stdin.flush()
        return self.receive()

    def crash(self):
        # Intentionally terminate only our owned test process without lifespan shutdown.
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class ControlledEngine:
    def __init__(self, root, hold_acknowledgement):
        self.root = root
        self.lock = threading.Lock()
        self.accepted = []
        self.persisted_before_send = []
        self.running = set()
        self.history = {}
        self.unexpected = []
        self.release_response = threading.Event()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                encoded = json.dumps(value).encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The original workbench process was deliberately crashed.

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/prompt':
                    with engine.lock:
                        engine.unexpected.append(self.path)
                    self.send_error(400)
                    return
                with sqlite3.connect(engine.root / 'tasks.sqlite3') as connection:
                    persisted = connection.execute('SELECT prompt_id FROM tasks WHERE id=?',
                                                   (payload['prompt_id'],)).fetchone()
                with engine.lock:
                    prompt_id = payload['prompt_id']
                    engine.persisted_before_send.append(persisted == (prompt_id,))
                    engine.accepted.append(payload)
                    engine.running.add(prompt_id)
                    first = len(engine.accepted) == 1
                if first and hold_acknowledgement:
                    engine.release_response.wait(30)
                self.reply({'prompt_id': prompt_id, 'number': len(engine.accepted), 'node_errors': {}})

            def do_GET(self):
                with engine.lock:
                    if self.path == '/queue':
                        result = {'queue_running': [[0, prompt_id] for prompt_id in engine.running], 'queue_pending': []}
                    elif self.path == '/system_stats':
                        result = {'system': {'ram_free': 8 * 2**30},
                                  'devices': [{'vram_free': 8 * 2**30, 'vram_total': 16 * 2**30}]}
                    elif self.path.startswith('/history/'):
                        prompt_id = self.path[len('/history/'):]
                        result = {prompt_id: engine.history[prompt_id]} if prompt_id in engine.history else {}
                    elif self.path == '/history?max_items=1':
                        result = dict(list(engine.history.items())[-1:])
                    else:
                        engine.unexpected.append(self.path)
                        self.send_error(400)
                        return
                self.reply(result)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def submissions(self):
        with self.lock:
            return [value['prompt_id'] for value in self.accepted]

    def forget_running(self, prompt_id):
        with self.lock:
            self.running.discard(prompt_id)

    def complete(self, prompt_id, *, create_output=True):
        # Receipt tests need an existing output, not a model or media-quality claim.
        with self.lock:
            submission = next(value for value in self.accepted if value['prompt_id'] == prompt_id)
        prefix = submission['prompt']['92']['inputs']['filename_prefix']
        relative = Path(prefix + '.mp4')
        assert not relative.is_absolute() and '..' not in relative.parts
        output = self.root / 'output' / relative
        if create_output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'controlled engine output; not a generated video')
        with self.lock:
            self.running.discard(prompt_id)
            self.history[prompt_id] = {'status': {'status_str': 'success', 'completed': True},
                'outputs': {'save': {'images': [{'filename': output.name,
                    'subfolder': relative.parent.as_posix(), 'type': 'output'}]}}}
        return relative.as_posix(), output.read_bytes() if create_output else None

    def close(self):
        self.release_response.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def wait_until(read, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(.05)


def http(worker, owner, method, path, status=200, **kwargs):
    response = worker.request(owner, method, path, **kwargs)
    assert response['status'] == status, response
    return response.get('body', response)


def prepare_project(worker, owner, shots):
    project = http(worker, owner, 'POST', '/api/projects/create', json={
        'title': 'Isolated', 'series': 'T', 'creation_mode': 'advanced'})['project']
    base = '/api/projects/' + project['id']
    http(worker, owner, 'POST', base + '/production-presets/h3-first-native-landscape')
    uploaded = http(worker, owner, 'POST', base + '/upload', upload=base64.b64encode(b'test-input').decode())
    first_frame = uploaded['uploaded'][0]
    for shot in shots:
        http(worker, owner, 'POST', base + '/plan/segments', json={
            'segment_id': shot, 'duration_seconds': 5, 'prompt': shot + ' test', 'first_frame': first_frame})
    return base


@pytest.mark.parametrize('hold_acknowledgement', [False, True], ids=['running', 'accepted-response-lost'])
def test_crash_reconciles_original_prompt_and_preserves_private_fair_queue(tmp_path, hold_acknowledgement):
    engine = ControlledEngine(tmp_path, hold_acknowledgement)
    worker = None
    try:
        worker = WorkbenchProcess(tmp_path, engine.url)
        abase = prepare_project(worker, 'user001', ['S01', 'S02', 'S03', 'S04'])
        bbase = prepare_project(worker, 'user002', ['S01', 'S02'])
        arequest = {'asset_ids': ['S01', 'S02', 'S03'], 'idempotency_key': 'a-batch'}
        brequest = {'asset_ids': ['S01', 'S02'], 'idempotency_key': 'b-batch'}
        a = http(worker, 'user001', 'POST', abase + '/batches', json=arequest)
        a1, a2, a3 = a['task_ids']
        wait_until(engine.submissions, lambda ids: ids == [a1])
        expected_state = 'submitting' if hold_acknowledgement else 'running'
        original = wait_until(lambda: http(worker, 'user001', 'GET', abase + '/tasks/' + a1),
                              lambda task: task['status'] == expected_state)
        assert original['prompt_id'] == a1
        b = http(worker, 'user002', 'POST', bbase + '/batches', json=brequest)
        b1, b2 = b['task_ids']
        cancel_request = {'asset_id': 'S04', 'idempotency_key': 'cancel-before-restart'}
        cancelled = http(worker, 'user001', 'POST', abase + '/tasks', json=cancel_request)
        http(worker, 'user001', 'POST', abase + '/tasks/' + cancelled['id'] + '/stop')
        with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
            before = {row[0]: row[1:] for row in connection.execute('SELECT id,status,prompt_id,payload FROM tasks')}
            meta = dict(connection.execute('SELECT key,value FROM scheduler_meta'))
        assert before[a1][:2] == (expected_state, a1)
        assert all(before[task][0] == 'batch_waiting' and before[task][1] is None for task in (a2, a3, b1, b2))
        assert before[cancelled['id']][0] == 'stopped'
        assert meta['last_gpu_user'] == 'user001'
        old_pid = worker.pid
        worker.crash()
        worker = WorkbenchProcess(tmp_path, engine.url)
        assert worker.pid != old_pid
        recovered = http(worker, 'user001', 'GET', abase + '/tasks/' + a1)
        assert recovered['status'] == 'needs_reconcile' and recovered['prompt_id'] == a1
        assert recovered['payload'] == json.loads(before[a1][2])
        with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
            after = {row[0]: row[1:] for row in connection.execute('SELECT id,status,prompt_id,payload FROM tasks')}
        assert set(after) == set(before)
        assert all(after[task] == before[task] for task in (a2, a3, b1, b2, cancelled['id']))
        assert http(worker, 'user001', 'GET', abase + '/tasks/' + cancelled['id'])['status'] == 'stopped'
        for owner, base, expected_ids, hidden in (
            ('user001', abase, {a1, a2, a3}, {b1, b2}), ('user002', bbase, {b1, b2}, {a1, a2, a3}),
        ):
            visible = http(worker, owner, 'GET', '/api/queue')
            ids = {task['id'] for task in visible['tasks']}
            assert expected_ids <= ids and not (ids & hidden)
        for method, suffix in (('GET', ''), ('POST', '/stop'), ('POST', '/resume'), ('POST', '/reconcile')):
            http(worker, 'user002', method, abase + '/tasks/' + a1 + suffix, status=404)
        assert http(worker, 'user001', 'POST', abase + '/batches', json=arequest)['task_ids'] == a['task_ids']
        assert http(worker, 'user002', 'POST', bbase + '/batches', json=brequest)['task_ids'] == b['task_ids']
        assert http(worker, 'user001', 'POST', abase + '/tasks', json=cancel_request)['id'] == cancelled['id']
        conflict = http(worker, 'user001', 'POST', abase + '/batches', status=409,
                        json={**arequest, 'asset_ids': ['S01']})
        assert conflict['detail']['code'] == 'idempotency_key_conflict'
        # New keys cannot bypass an unresolved execution for the same segment.
        blocked = http(worker, 'user001', 'POST', abase + '/tasks', status=409,
                       json={'asset_id': 'S01', 'idempotency_key': 'not-the-original-key'})
        assert blocked['detail']['code'] == 'asset_task_pending'
        for suffix in ('/reconcile', '/resume'):
            http(worker, 'user001', 'POST', abase + '/tasks/' + a1 + suffix, status=409)
        assert engine.submissions() == [a1]
        # Empty engine queue alone is not evidence that the original job failed.
        engine.forget_running(a1)
        for suffix in ('/reconcile', '/resume'):
            http(worker, 'user001', 'POST', abase + '/tasks/' + a1 + suffix, status=409)
        waiting = wait_until(lambda: http(worker, 'user002', 'GET', bbase + '/tasks/' + b1),
                             lambda task: task['queue']['reason_code'] == 'waiting_reconciliation')
        assert waiting['status'] == 'batch_waiting' and waiting['queue']['position'] == 1
        assert http(worker, 'user001', 'GET', abase + '/tasks/' + a2)['queue']['position'] == 2
        assert engine.submissions() == [a1]
        # A success status without its output file also cannot release the slot.
        engine.complete(a1, create_output=False)
        for suffix in ('/reconcile', '/resume'):
            http(worker, 'user001', 'POST', abase + '/tasks/' + a1 + suffix, status=409)
        assert http(worker, 'user001', 'GET', abase + '/tasks/' + a1)['status'] == 'needs_reconcile'
        assert engine.submissions() == [a1]
        output_path, output = engine.complete(a1)
        resolved = http(worker, 'user001', 'POST', abase + '/tasks/' + a1 + '/reconcile')
        assert resolved['id'] == a1 and resolved['prompt_id'] == a1 and resolved['status'] == 'succeeded'
        assert resolved['result']['outputs'] == [output_path]
        own_output = resolved['result']['published_outputs'][0]
        media = http(worker, 'user001', 'GET', '/media-file', params={'path': own_output})
        assert base64.b64decode(media['bytes']) == output
        http(worker, 'user002', 'GET', '/media-file', status=403, params={'path': own_output})
        # The last dispatched owner survives restart: B1, A2, B2, A3.
        for index, (owner, base, task_id) in enumerate((
            ('user002', bbase, b1), ('user001', abase, a2), ('user002', bbase, b2), ('user001', abase, a3),
        ), start=2):
            accepted = wait_until(engine.submissions, lambda ids: len(ids) >= index)
            assert accepted[-1] == task_id and len(accepted) == index
            engine.complete(task_id)
            wait_until(lambda: http(worker, owner, 'GET', base + '/tasks/' + task_id),
                       lambda task: task['status'] == 'succeeded')
        assert engine.submissions() == [a1, b1, a2, b2, a3]
        assert len(set(engine.submissions())) == 5
        assert engine.persisted_before_send == [True] * 5
        assert not engine.unexpected
        assert http(worker, 'user001', 'POST', abase + '/tasks/' + a1 + '/reconcile')['id'] == a1
        assert http(worker, 'user001', 'POST', abase + '/batches', json=arequest)['task_ids'] == a['task_ids']
        plan = http(worker, 'user001', 'GET', abase + '/plan')
        segment = next(item for item in plan['segments'] if item['id'] == 'S01')
        assert [item['task_id'] for item in segment['generation_history']] == [a1]
        with sqlite3.connect(tmp_path / 'tasks.sqlite3') as connection:
            assert connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 6
    finally:
        if worker is not None:
            worker.crash()
        engine.close()


if __name__ == '__main__':
    assert sys.argv[1] == '--worker'
    workbench_process(Path(sys.argv[2]).resolve(), sys.argv[3])
