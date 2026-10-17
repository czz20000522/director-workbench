import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend import app as backend
from test_task_reconciliation import reconcile_client

REAL_THREAD = threading.Thread
REAL_REQUEST = backend.request_json
REAL_HISTORY = backend.history_done


def test_real_http_disconnect_recovers_without_second_submission(reconcile_client, monkeypatch, tmp_path):
    client, plan = reconcile_client
    monkeypatch.setattr(threading, 'Thread', REAL_THREAD)
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert backend.get_task('old')['prompt_id'] == payload['prompt_id']
            received.append(payload)
            # The executor accepted the graph; the response never arrives.
            self.close_connection = True

        def do_GET(self):
            if self.path == '/queue':
                value = {'queue_running': [], 'queue_pending': []}
            else:
                prompt = received[0]['prompt_id']
                value = {prompt: {'status': {'status_str': 'success'}, 'outputs': {'save': {'images': [{'filename': 'result.mp4', 'type': 'output'}]}}}}
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = REAL_THREAD(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(backend, 'COMFY_URL', f'http://127.0.0.1:{server.server_port}')
        monkeypatch.setattr(backend, 'request_json', REAL_REQUEST)
        monkeypatch.setattr(backend, 'history_done', REAL_HISTORY)
        graph = tmp_path / 'graph.json'
        graph.write_text('{}')
        task = backend.get_task('old')
        payload = {**task['payload'], 'workflow': str(graph)}
        backend.set_task('old', prompt_id=None, payload=json.dumps(payload))
        backend.run_workflow_submission('old', payload)
        assert backend.get_task('old')['status'] == 'needs_reconcile'
        assert backend.get_task('old')['prompt_id'] == 'old'
        response = client.post('/api/tasks/old/resume')
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'succeeded'
        assert response.json()['id'] == 'old'
        assert len(received) == 1
        assert len(client.get('/api/tasks').json()) == 1
        segment = json.loads(plan.read_text(encoding='utf-8'))['segments'][0]
        assert segment['video']['path'] == 'output/result.mp4'
        assert segment['prompt'] == 'retain director notes'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
